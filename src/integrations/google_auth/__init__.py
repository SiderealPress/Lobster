"""Google OAuth helpers shared across Calendar and Gmail integrations.

This module provides the `generate_consent_link` function, which asks
myownlobster.ai to mint a one-time consent URL for a given OAuth scope.
The VPS never holds GCP credentials; it delegates that responsibility to
the myownlobster.ai web app.
"""

from integrations.google_auth.consent import generate_consent_link

__all__ = ["generate_consent_link"]
