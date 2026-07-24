"""
oauth_vault — provider-parameterized OAuth token storage and refresh (BIS-732 / Slice 4).

Extracts the pattern proven identically three times across
``integrations.google_calendar.token_store``, ``integrations.gmail.token_store``,
and ``integrations.google_workspace.token_store`` (per-user local-disk token
persistence, atomic 0600 writes, myownlobster.ai refresh-proxy calls, and the
BIS-731 workspace-token fallback) into a single module parameterized by
``provider`` instead of copy-pasted per integration.

Submodules:
    vault_store — pure serialization + file I/O, parameterized by an explicit
                  ``token_dir`` (no provider-registry knowledge).
    client       — provider registry, refresh-proxy HTTP call, and the
                  higher-level ``get_valid_token(provider, user_id, ...)``
                  that composes vault_store with refresh + workspace fallback.

This slice cuts over ``google_calendar/client.py`` only, proving the
abstraction against the best-tested existing integration before Gmail and
Workspace follow in later slices. The three original ``token_store.py``
modules are left in the tree, unimported by this package, as a one-release
rollback path.
"""

from __future__ import annotations
