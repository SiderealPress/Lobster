"""
Granola multi-account support — shared helpers for polling multiple API keys.

Provides:
- AccountConfig: typed descriptor for a Granola account
- AccountRegistry: strict dict-like lookup from account name → AccountConfig
- build_accounts_from_env: discover configured accounts from environment
- annotate_note_with_account: pure function adding 'account' field to a raw note dict
- merge_and_deduplicate: combine notes from multiple accounts, deduplicate by ID

Design principles:
- Pure functions — no I/O, no side effects
- Immutable inputs — original dicts are never mutated
- Deterministic deduplication — primary account always wins on conflict

Constants:
    ACCOUNT_PRIMARY   = "primary"   (primary, uses GRANOLA_API_KEY)
    ACCOUNT_SECONDARY = "secondary" (secondary, uses GRANOLA_API_KEY_2)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Account names (constants, not magic strings)
# ---------------------------------------------------------------------------

ACCOUNT_PRIMARY: str = "primary"
ACCOUNT_SECONDARY: str = "secondary"

# Environment variable names
_ENV_KEY_PRIMARY: str = "GRANOLA_API_KEY"
_ENV_KEY_SECONDARY: str = "GRANOLA_API_KEY_2"


# ---------------------------------------------------------------------------
# AccountConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountConfig:
    """
    Immutable descriptor for a single Granola account.

    Structurally equivalent to GranolaAccountConfig in
    src/integrations/granola/client.py (name + api_key). The two types exist
    in separate subsystems that cannot share a cross-path import at this time.

    Attributes:
        name:    Account identifier ("primary" or "secondary").
        api_key: Bearer token for this account.
    """

    name: str
    api_key: str


# ---------------------------------------------------------------------------
# AccountRegistry — strict lookup, no silent fallback
# ---------------------------------------------------------------------------


class AccountRegistry:
    """
    Immutable registry mapping account names to AccountConfig objects.

    Raises KeyError (with a descriptive message) when an account name is
    looked up that has no registered key — preventing the silent primary-key
    fallback that existed when callers used dict.get() directly.

    Usage:
        registry = AccountRegistry(build_accounts_from_env(os.environ))
        cfg = registry.lookup("primary")   # → AccountConfig
        cfg = registry.lookup("ghost")     # → KeyError: "No API key registered for 'ghost'"

        "primary" in registry        # → True
    """

    def __init__(self, accounts: list[AccountConfig]) -> None:
        self._by_name: dict[str, AccountConfig] = {a.name: a for a in accounts}

    def lookup(self, account_name: str) -> AccountConfig:
        """
        Return the AccountConfig for account_name.

        Named ``lookup`` (not ``get``) to signal that this raises on missing keys,
        unlike the standard Python dict.get() which returns None.

        Raises:
            KeyError: if account_name is not registered.
        """
        if account_name not in self._by_name:
            raise KeyError(
                f"No API key registered for Granola account {account_name!r}. "
                f"Add its key to ~/lobster-config/config.env."
            )
        return self._by_name[account_name]

    def __contains__(self, account_name: object) -> bool:
        return account_name in self._by_name

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)


# ---------------------------------------------------------------------------
# build_accounts_from_env
# ---------------------------------------------------------------------------


def build_accounts_from_env(env: dict[str, str]) -> list[AccountConfig]:
    """
    Discover configured Granola accounts from an environment dict.

    Rules:
    - GRANOLA_API_KEY is required (primary account).
    - GRANOLA_API_KEY_2 is optional (secondary account).
    - Primary account is always first in the returned list.
    - If GRANOLA_API_KEY is absent, an empty list is returned.

    Args:
        env: Dict of environment variables (typically os.environ or a subset).

    Returns:
        List of AccountConfig, primary account first.
    """
    primary_key = env.get(_ENV_KEY_PRIMARY, "").strip()
    if not primary_key:
        return []

    accounts: list[AccountConfig] = [
        AccountConfig(name=ACCOUNT_PRIMARY, api_key=primary_key),
    ]

    secondary_key = env.get(_ENV_KEY_SECONDARY, "").strip()
    if secondary_key:
        accounts.append(AccountConfig(name=ACCOUNT_SECONDARY, api_key=secondary_key))

    return accounts


# ---------------------------------------------------------------------------
# annotate_note_with_account
# ---------------------------------------------------------------------------


def annotate_note_with_account(note: dict, account_name: str) -> dict:
    """
    Return a new dict with the 'account' field added.

    The original dict is never mutated.

    Args:
        note:         Raw note dict from the Granola API.
        account_name: Account identifier string (e.g. ACCOUNT_PRIMARY).

    Returns:
        New dict with all original fields plus 'account': account_name.
    """
    return {**note, "account": account_name}


# ---------------------------------------------------------------------------
# merge_and_deduplicate
# ---------------------------------------------------------------------------


def merge_and_deduplicate(
    primary_notes: list[dict],
    secondary_notes: list[dict],
) -> list[dict]:
    """
    Merge notes from two accounts, deduplicating by note ID.

    Primary account notes take precedence: if the same note ID appears in
    both accounts, the primary version is kept and the secondary is dropped.

    Args:
        primary_notes:   Notes from the primary account (already annotated).
        secondary_notes: Notes from the secondary account (already annotated).

    Returns:
        Merged list with no duplicate IDs. Primary notes appear first,
        followed by secondary-only notes.
    """
    # Build a set of note IDs already covered by the primary account
    primary_ids: set[str] = {n["id"] for n in primary_notes}

    # Keep only secondary notes whose ID is not already in the primary set
    secondary_unique = [n for n in secondary_notes if n["id"] not in primary_ids]

    return list(primary_notes) + secondary_unique
