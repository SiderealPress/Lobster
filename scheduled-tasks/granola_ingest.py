"""
Granola Ingest — Main Entry Point (Slices 1-4)

Incremental ingest of Granola meeting notes into the versioned folder
hierarchy. Designed to run every 15 minutes from cron.

Supports multiple Granola accounts:
- GRANOLA_API_KEY   — primary account (required)
- GRANOLA_API_KEY_2 — secondary account (optional)

When both keys are present, notes from both accounts are fetched and merged.
Each account's cursor state is tracked independently (keyed by account name).
Notes from both accounts are deduplicated by note ID (primary account wins).
Each note has an 'account' field added to its stored JSON ('primary' or 'secondary').

Exit codes:
  0 — success (including 0 new notes)
  1 — partial failure (API error, some notes may not have been written)
  2 — fatal / configuration error (missing API key, etc.)

Logs are written to ~/lobster-workspace/granola-notes/ingest.log
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Bootstrap: ensure we can import sibling modules regardless of cwd
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent.resolve()
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from granola_client import GranolaClient, GranolaAPIError  # noqa: E402
from granola_state import IncrementalFetcher, StateManager  # noqa: E402
from granola_storage import NoteStorage  # noqa: E402
from granola_multi_account import (  # noqa: E402
    AccountConfig,
    build_accounts_from_env,
    annotate_note_with_account,
    merge_and_deduplicate,
    ACCOUNT_PRIMARY,
    ACCOUNT_SECONDARY,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

NOTES_ROOT = Path.home() / "lobster-workspace" / "granola-notes"
LOG_PATH = NOTES_ROOT / "ingest.log"


def _setup_logging() -> logging.Logger:
    NOTES_ROOT.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("granola_ingest")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # File handler (append)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Load config.env into environment
# ---------------------------------------------------------------------------

def _load_config_env() -> None:
    """Source ~/lobster-config/config.env into os.environ if not already set."""
    config_dir = os.environ.get("LOBSTER_CONFIG_DIR", str(Path.home() / "lobster-config"))
    config_path = Path(config_dir) / "config.env"
    if not config_path.exists():
        return
    with config_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Per-account fetch with per-account cursor state
# ---------------------------------------------------------------------------

def _fetch_notes_for_account(
    account: AccountConfig,
    log: logging.Logger,
) -> list[dict]:
    """
    Fetch all new notes for a single Granola account.

    Uses an account-namespaced state key inside the shared .state.json file
    so each account's cursor is tracked independently.

    Returns a list of annotated raw note dicts (with 'account' field added).
    """
    # Each account gets its own StateManager pointing to the same file but
    # using a namespaced state_path suffix to isolate cursors.
    account_state_path = NOTES_ROOT / f".state-{account.name}.json"
    state_manager = StateManager(state_path=account_state_path)

    client = GranolaClient(api_key=account.api_key)
    fetcher = IncrementalFetcher(client=client, state_manager=state_manager)

    notes: list[dict] = []
    for note in fetcher.fetch_new():
        annotated = annotate_note_with_account(note.raw, account.name)
        notes.append(annotated)

    log.info(
        "Account '%s': fetched %d new notes",
        account.name,
        len(notes),
    )
    return notes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log = _setup_logging()
    log.info("=== Granola ingest started ===")

    _load_config_env()

    # Discover configured accounts
    accounts = build_accounts_from_env(dict(os.environ))
    if not accounts:
        log.error(
            "GRANOLA_API_KEY not set. Set it in ~/lobster-config/config.env "
            "or export it before running."
        )
        return 2

    account_names = ", ".join(a.name for a in accounts)
    log.info("Polling %d account(s): %s", len(accounts), account_names)

    storage = NoteStorage(root=NOTES_ROOT)

    # Fetch notes per account
    all_notes_by_account: dict[str, list[dict]] = {}
    for account in accounts:
        try:
            all_notes_by_account[account.name] = _fetch_notes_for_account(account, log)
        except GranolaAPIError as exc:
            log.error("Granola API error for account '%s': %s", account.name, exc)
            # Treat a single account failure as partial — continue with others
            all_notes_by_account[account.name] = []
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Unexpected error fetching account '%s': %s",
                account.name, exc, exc_info=True,
            )
            all_notes_by_account[account.name] = []

    # Merge and deduplicate across accounts
    primary_notes = all_notes_by_account.get(ACCOUNT_PRIMARY, [])
    secondary_notes = all_notes_by_account.get(ACCOUNT_SECONDARY, [])
    merged_notes = merge_and_deduplicate(primary_notes, secondary_notes)

    log.info(
        "Merged: %d from primary, %d from secondary → %d after dedup",
        len(primary_notes), len(secondary_notes), len(merged_notes),
    )

    # Write merged notes to storage
    written = 0
    skipped = 0
    errors = 0

    for raw_note in merged_notes:
        try:
            path, was_new = storage.write_note(raw_note)
            if was_new:
                written += 1
                log.debug("Wrote note %s → %s", raw_note.get("id"), path)
            else:
                skipped += 1
                log.debug("Skipped (identical) note %s", raw_note.get("id"))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("Failed to write note %s: %s", raw_note.get("id"), exc)

    log.info(
        "Ingest complete — wrote %d new, skipped %d identical, errors %d",
        written, skipped, errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
