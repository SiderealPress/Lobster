"""generate_consent_link — VPS side of the OAuth consent-link flow.

The VPS never holds GCP client credentials.  Instead it calls
``POST https://myownlobster.ai/api/generate-consent-link``, which:

1. Validates the shared secret (LOBSTER_INTERNAL_SECRET).
2. Creates a short-lived (30-min) one-time token keyed by scope +
   instance_url.
3. Returns the full consent URL for the user to visit.

The caller (skill handler, MCP tool, etc.) sends that URL to the user;
the user clicks it, grants consent in their browser, and myownlobster.ai
pushes the resulting tokens back to the VPS via
``/api/push-calendar-token`` or ``/api/push-gmail-token``.

Design principles
-----------------
- ``generate_consent_link`` is a pure function of its inputs + env;
  the only side effect is the outbound HTTP POST.
- Secrets never appear in log output (we log scope and instance_url
  but not the secret itself).
- Configuration is read from environment variables at call time so that
  the function is trivially testable by patching ``os.environ``.

Environment variables required
-------------------------------
LOBSTER_INSTANCE_URL
    The public base URL of this Lobster VPS instance
    (e.g. ``https://vps.example.com``).  myownlobster.ai uses this to
    route the token-push callback back to the correct instance.

LOBSTER_INTERNAL_SECRET
    The shared secret that authenticates VPS→myownlobster requests.
    Must match the ``LOBSTER_INTERNAL_SECRET`` Vercel env var on the
    myownlobster.ai deployment.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONSENT_ENDPOINT: str = "https://myownlobster.ai/api/generate-consent-link"
_HTTP_TIMEOUT: int = 10

_VALID_SCOPES: frozenset[str] = frozenset({"calendar", "gmail"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_consent_link(scope: str) -> str:
    """Call myownlobster.ai to generate a one-time consent URL.

    Args:
        scope: ``"calendar"`` or ``"gmail"``.  The myownlobster.ai endpoint
               uses this to select the correct Google OAuth scopes and to
               build the per-scope redirect path
               (``/connect/calendar?token=…`` or ``/connect/gmail?token=…``).

    Returns:
        The full one-time consent URL to send to the user.

    Raises:
        ValueError: If ``scope`` is not ``"calendar"`` or ``"gmail"``.
        RuntimeError: If ``LOBSTER_INSTANCE_URL`` or
            ``LOBSTER_INTERNAL_SECRET`` are not set in the environment.
        RuntimeError: If the myownlobster.ai endpoint returns a non-2xx
            response or an unexpected payload.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"Invalid scope {scope!r}. Must be one of: {sorted(_VALID_SCOPES)}"
        )

    instance_url, instance_secret = _read_env()

    log.info(
        "Requesting consent link from myownlobster.ai: scope=%r instance_url=%r",
        scope,
        instance_url,
    )

    url = _post_generate_consent_link(
        scope=scope,
        instance_url=instance_url,
        instance_secret=instance_secret,
    )

    log.info(
        "Consent link obtained from myownlobster.ai: scope=%r url_prefix=%r",
        scope,
        url[:60] if url else "",
    )
    return url


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_env() -> tuple[str, str]:
    """Read and validate required environment variables.

    Returns:
        ``(instance_url, instance_secret)`` — both stripped of whitespace.

    Raises:
        RuntimeError: If either variable is absent or empty.
    """
    instance_url = os.environ.get("LOBSTER_INSTANCE_URL", "").strip()
    instance_secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "").strip()

    missing = [
        name
        for name, value in (
            ("LOBSTER_INSTANCE_URL", instance_url),
            ("LOBSTER_INTERNAL_SECRET", instance_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Both LOBSTER_INSTANCE_URL and LOBSTER_INTERNAL_SECRET must be set "
            "to generate a consent link."
        )

    return instance_url, instance_secret


def _post_generate_consent_link(
    scope: str,
    instance_url: str,
    instance_secret: str,
    endpoint: str = _CONSENT_ENDPOINT,
    timeout: int = _HTTP_TIMEOUT,
) -> str:
    """POST to myownlobster.ai and extract the consent URL.

    Separated from ``generate_consent_link`` so the HTTP call is injectable
    in tests without patching the entire function.

    Args:
        scope:           ``"calendar"`` or ``"gmail"``.
        instance_url:    Public base URL of this Lobster instance.
        instance_secret: Shared secret (sent in the JSON body, not a header,
                         so that the myownlobster.ai route handler can validate
                         it before issuing the token).
        endpoint:        Full URL of the myownlobster.ai endpoint
                         (injectable for testing).
        timeout:         HTTP request timeout in seconds.

    Returns:
        The consent URL string from the ``"url"`` key of the JSON response.

    Raises:
        RuntimeError: On HTTP error or unexpected response schema.
    """
    try:
        resp = requests.post(
            endpoint,
            json={
                "scope": scope,
                "instance_url": instance_url,
                "instance_secret": instance_secret,
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Failed to reach myownlobster.ai consent-link endpoint: {exc}"
        ) from exc

    if not resp.ok:
        raise RuntimeError(
            f"myownlobster.ai returned HTTP {resp.status_code} "
            f"for generate-consent-link (scope={scope!r}): {resp.text[:200]}"
        )

    try:
        data = resp.json()
        url: str = data["url"]
    except (ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Unexpected response from myownlobster.ai consent-link endpoint: {exc}. "
            f"Body: {resp.text[:200]}"
        ) from exc

    if not url:
        raise RuntimeError(
            "myownlobster.ai returned an empty consent URL "
            f"for scope={scope!r}."
        )

    return url
