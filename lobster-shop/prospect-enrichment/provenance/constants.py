"""
Shared provenance constants for the prospect-enrichment pipeline.

Single source of truth for the `provenance.enriched_by` value written to
every Kissinger enrichment record. Configure via LOBSTER_ASSISTANT_NAME;
defaults to "wallace" to match this deployment's existing data so historical
records and new writes stay consistent unless the operator overrides it.

Import this instead of hardcoding the assistant name or repeating
`os.environ.get("LOBSTER_ASSISTANT_NAME", ...)` in every enrichment script.
"""

from __future__ import annotations

import os

ASSISTANT_NAME: str = os.environ.get("LOBSTER_ASSISTANT_NAME", "wallace")
