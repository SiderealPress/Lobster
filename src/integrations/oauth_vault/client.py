"""
oauth_vault.client — provider-parameterized get_valid_token + refresh proxy
(BIS-732 / Slice 4).

Generalizes the ``get_valid_token`` workflow proven identically three times
across ``google_calendar/token_store.py``, ``gmail/token_store.py``, and
``google_workspace/token_store.py`` — load local token -> return if still
valid -> refresh via the myownlobster.ai proxy if expired -> persist -> the
BIS-731 workspace-token fallback when no scope-specific token exists at all
— into one implementation parameterized by a ``provider`` key instead of
copy-pasted per integration.

**Deliberate deviation from the literal Slice 4 plan text, flagged here:**
the plan describes the new canonical storage path as
``~/messages/config/oauth-tokens/<provider>/<chat_id>.json`` for every
provider uniformly. For the "calendar" provider specifically, this module
instead aliases its default ``token_dir`` to the *existing*
``~/messages/config/gcal-tokens/`` directory (see ``PROVIDERS["calendar"]``
below) rather than the fresh ``oauth-tokens/calendar/`` path. Reason: the
*write* path for Calendar tokens — ``push_calendar_token_endpoint`` in
``inbox_server_http.py`` (the myownlobster.ai push-token receiver) and the
local ``callback_server.py`` flow — is explicitly NOT part of this slice's
cutover and continues to write to ``gcal-tokens/`` via the old
``google_calendar/token_store.py``. Pointing this module's read path at a
new, empty ``oauth-tokens/calendar/`` directory instead would mean a real,
already-granted Calendar token silently stops being found the moment
``google_calendar/client.py`` cuts over — exactly the kind of regression
Slice 4's "re-run 'what's on my calendar' ... identical behavior" manual
check exists to catch. The fresh ``oauth-tokens/<provider>/`` layout
remains the target for any new provider with no legacy directory to alias
to, and becomes the actual location for calendar once its write path is
cut over too (tracked for a later slice, not created here).

This module is additive: the three existing token_store.py modules are left
in the tree, unimported by this module, as a one-release rollback path —
deleted only once every consumer (read AND write paths) has migrated.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from integrations.google_calendar.oauth import TokenData, is_token_valid
from integrations.google_workspace import token_store as _workspace_token_store
from integrations.google_workspace.config import WORKSPACE_TOKEN_DIR
from integrations.oauth_vault.vault_store import (
    load_token as _load_token,
    provider_token_dir,
    save_token as _save_token,
)

log = logging.getLogger(__name__)

_HOME: Path = Path.home()
_MESSAGES_DIR: Path = Path(os.environ.get("LOBSTER_MESSAGES", str(_HOME / "messages")))

# HTTP timeout for refresh proxy calls (seconds) — same as all three
# predecessor token_store.py modules.
_HTTP_TIMEOUT: int = 10

# Default refresh proxy base URL (GCP secrets live on myownlobster.ai).
_DEFAULT_API_BASE: str = "https://myownlobster.ai"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderConfig:
    """Per-provider settings needed to resolve a valid access token.

    Attributes:
        name:                      Provider key, e.g. "calendar".
        token_dir:                 Directory this provider's tokens live in.
                                    See module docstring re: why this aliases
                                    to a legacy directory for "calendar"
                                    rather than using ``provider_token_dir``.
        refresh_endpoint:          myownlobster.ai internal refresh path.
        config_path:               Path to this provider's own JSON config
                                    file (``myownlobster_api_base`` override)
                                    — kept per-provider (not a single shared
                                    vault config) so the cutover preserves
                                    prior behavior file-for-file: an operator
                                    who already overrode
                                    ``calendar-config.json`` sees no change.
        workspace_scope_substring: If set, ``get_valid_token`` falls back to
                                    the shared workspace token store when
                                    this provider's own token file is absent,
                                    accepting the workspace token only if its
                                    granted scope contains this substring
                                    (the BIS-731 pattern, generalized).
    """

    name: str
    token_dir: Path
    refresh_endpoint: str
    config_path: Path
    workspace_scope_substring: Optional[str] = None


PROVIDERS: dict[str, ProviderConfig] = {
    "calendar": ProviderConfig(
        name="calendar",
        # Alias to the pre-existing gcal-tokens/ directory — see the
        # "Deliberate deviation" note in this module's docstring.
        token_dir=_MESSAGES_DIR / "config" / "gcal-tokens",
        refresh_endpoint="/api/internal/refresh-calendar-token",
        config_path=_MESSAGES_DIR / "config" / "calendar-config.json",
        workspace_scope_substring="calendar",
    ),
}


def _provider_config(provider: str) -> ProviderConfig:
    """Look up a registered provider's config, or raise a clear error."""
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown oauth_vault provider {provider!r}. "
            f"Registered providers: {sorted(PROVIDERS)}"
        ) from None


# ---------------------------------------------------------------------------
# Per-provider config loader (myownlobster_api_base override)
# ---------------------------------------------------------------------------


def _load_provider_config_file(provider_cfg: ProviderConfig) -> dict:
    """Return the parsed provider config JSON, or an empty dict if absent."""
    if not provider_cfg.config_path.exists():
        return {}
    try:
        return json.loads(provider_cfg.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(
            "oauth_vault: failed to parse config for provider=%r: %s",
            provider_cfg.name, exc,
        )
        return {}


def _myownlobster_api_base(provider_cfg: ProviderConfig) -> str:
    """Return the myownlobster API base URL from provider config, or the default."""
    config = _load_provider_config_file(provider_cfg)
    return config.get("myownlobster_api_base", _DEFAULT_API_BASE).rstrip("/")


# ---------------------------------------------------------------------------
# Auth header helper
# ---------------------------------------------------------------------------


def _internal_auth_header() -> dict[str, str]:
    """Return the Authorization header for internal API calls.

    Reads LOBSTER_INTERNAL_SECRET from the environment.

    Raises:
        RuntimeError: If LOBSTER_INTERNAL_SECRET is not set.
    """
    secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "LOBSTER_INTERNAL_SECRET is not set. "
            "Add it to config.env to enable token refresh via myownlobster."
        )
    return {"Authorization": f"Bearer {secret}"}


# ---------------------------------------------------------------------------
# Refresh proxy (calls myownlobster.ai — side-effecting)
# ---------------------------------------------------------------------------


def _refresh_token_via_proxy(provider: str, refresh_token: str) -> Optional[TokenData]:
    """Obtain a new access token by calling the myownlobster refresh proxy.

    myownlobster.ai holds the GCP client_id + client_secret and proxies the
    refresh call to Google, returning only the new access_token and expires_in.

    Args:
        provider:      Registered provider key (e.g. "calendar").
        refresh_token: The long-lived refresh token.

    Returns:
        A new TokenData (refresh_token preserved from caller), or None on error.
    """
    provider_cfg = _provider_config(provider)
    api_base = _myownlobster_api_base(provider_cfg)
    url = f"{api_base}{provider_cfg.refresh_endpoint}"

    try:
        headers = _internal_auth_header()
    except RuntimeError as exc:
        log.error("oauth_vault refresh proxy (%s): %s", provider, exc)
        return None

    try:
        resp = requests.post(
            url,
            json={"refresh_token": refresh_token},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("oauth_vault refresh proxy (%s) unreachable: %s", provider, exc)
        return None

    if not resp.ok:
        log.warning(
            "oauth_vault refresh proxy (%s) returned %d: %s",
            provider, resp.status_code, resp.text[:200],
        )
        return None

    try:
        data = resp.json()
        access_token = data["access_token"]
        expires_in = int(data["expires_in"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning(
            "oauth_vault refresh proxy (%s) returned unexpected payload: %s",
            provider, exc,
        )
        return None

    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    log.info("oauth_vault: token refresh via proxy succeeded (provider=%s).", provider)
    return TokenData(
        access_token=access_token,
        expires_at=expires_at,
        scope="",  # scope is not returned by the refresh proxy; preserved from disk
        refresh_token=None,  # caller must preserve the original refresh_token
    )


# ---------------------------------------------------------------------------
# Workspace-token fallback (BIS-731 pattern, generalized)
# ---------------------------------------------------------------------------


def _get_workspace_fallback_token(
    provider_cfg: ProviderConfig,
    user_id: str,
    workspace_token_dir: Path,
) -> Optional[TokenData]:
    """Fall back to the shared Google Workspace token store, if applicable.

    Only attempted for providers that declare a ``workspace_scope_substring``.
    A user who ran the `workspace` consent flow (not this provider's own)
    already holds a token whose scope bundle includes this provider's scope
    "for unified-token support" (google_workspace/config.py WORKSPACE_SCOPES).

    Args:
        provider_cfg:         The requesting provider's config.
        user_id:              Unique identifier for the user.
        workspace_token_dir:  Workspace token directory (injectable for testing).

    Returns:
        The workspace TokenData if it exists, is valid (or refreshable), and
        its scope includes this provider's required substring; otherwise None.
    """
    if provider_cfg.workspace_scope_substring is None:
        return None

    workspace_token = _workspace_token_store.get_valid_token(
        user_id, token_dir=workspace_token_dir
    )
    if workspace_token is None:
        return None

    if provider_cfg.workspace_scope_substring not in workspace_token.scope:
        log.info(
            "oauth_vault: workspace token for user_id=%r does not grant %r scope "
            "(scope=%r) — not using it as a %s fallback.",
            user_id, provider_cfg.workspace_scope_substring,
            workspace_token.scope, provider_cfg.name,
        )
        return None

    log.info(
        "oauth_vault: no %s-specific token for user_id=%r — using workspace "
        "token fallback (scope includes %r).",
        provider_cfg.name, user_id, provider_cfg.workspace_scope_substring,
    )
    return workspace_token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_valid_token(
    provider: str,
    user_id: str,
    token_dir: Optional[Path] = None,
    credentials=None,  # kept for API compatibility with existing call sites; unused
    workspace_token_dir: Path = WORKSPACE_TOKEN_DIR,
) -> Optional[TokenData]:
    """Return a valid access token for (provider, user_id), refreshing if necessary.

    Workflow (identical to the three predecessor token_store.py modules):
    1. Load token from local disk (``token_dir``, or the provider's
       registered default if not given).
    2. If no token -> fall back to the workspace token store (BIS-731); if
       that also yields nothing usable -> return None (user must
       re-authenticate).
    3. If token is still valid -> return it.
    4. If token is expired -> call myownlobster refresh proxy.
    5. Persist the refreshed token (preserving the original refresh_token).
    6. If refresh fails -> log and return None.

    Args:
        provider:            Registered provider key (e.g. "calendar").
        user_id:             Unique identifier for the user (Telegram chat_id as str).
        token_dir:           Local token directory (injectable for testing).
                             Defaults to the provider's registered directory.
        credentials:         Ignored; kept for backwards-compatible call sites.
        workspace_token_dir: Workspace token directory (injectable for testing).
                             Only consulted when no provider-specific token
                             file exists at all — an existing provider token
                             is always used as-is, workspace is never checked.

    Returns:
        A valid TokenData, or None if no valid token is available.

    Raises:
        ValueError: If ``provider`` is not a registered provider.
    """
    provider_cfg = _provider_config(provider)
    effective_token_dir = token_dir if token_dir is not None else provider_cfg.token_dir

    token = _load_token(user_id, effective_token_dir)
    if token is None:
        log.info(
            "oauth_vault: no local token found for provider=%s user_id=%r.",
            provider, user_id,
        )
        return _get_workspace_fallback_token(provider_cfg, user_id, workspace_token_dir)

    if is_token_valid(token):
        return token

    # Token is expired — attempt refresh via myownlobster proxy
    if token.refresh_token is None:
        log.warning(
            "oauth_vault: token for provider=%s user_id=%r is expired and has "
            "no refresh_token; user must re-authenticate.",
            provider, user_id,
        )
        return None

    log.info(
        "oauth_vault: access token expired for provider=%s user_id=%r — "
        "refreshing via proxy.",
        provider, user_id,
    )

    refreshed_partial = _refresh_token_via_proxy(provider, token.refresh_token)
    if refreshed_partial is None:
        log.error(
            "oauth_vault: token refresh failed for provider=%s user_id=%r — "
            "user must re-authenticate.",
            provider, user_id,
        )
        return None

    # Merge: preserve scope, refresh_token, and email from the stored token
    refreshed = TokenData(
        access_token=refreshed_partial.access_token,
        expires_at=refreshed_partial.expires_at,
        scope=token.scope,                  # preserve original scope
        refresh_token=token.refresh_token,  # Google doesn't return new refresh_token here
        email=token.email,                  # preserve identity metadata across refresh
    )

    _save_token(user_id, refreshed, effective_token_dir)
    return refreshed
