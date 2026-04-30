"""
Granola → Obsidian incremental sync — Slice 3.

Entry point for the scheduled job. Reads the last-sync timestamp from
state file, fetches only notes updated since then (or all notes on first
run), writes to the Obsidian vault, git-commits, and updates state.

State file: ~/lobster-workspace/data/granola-sync-state.json
Vault path: ~/lobster-workspace/obsidian-vault/

Usage (standalone):
    cd ~/lobster
    uv run python -m integrations.granola.sync

Usage (as scheduled job, called by Lobster cron system):
    The scheduled task markdown file instructs the agent to run this script.

Output:
    Writes a structured result dict to stdout (JSON).
    Also calls write_task_output via the lobster-inbox HTTP API if
    LOBSTER_INBOX_URL env var is set.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Add src/ to path when run as a script
_SRC_DIR = Path(__file__).parent.parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from integrations.granola.client import (
    GranolaAPIError,
    GranolaAuthError,
    GranolaNote,
    GranolaAccountConfig,
    GranolaUnknownAccountError,
    build_account_configs_from_env,
    iter_all_notes_for_account,
    get_note,
    ACCOUNT_PRIMARY,
    ACCOUNT_SECONDARY,
)
from integrations.granola.vault_writer import write_notes_batch, WriteResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_STATE_FILE = _WORKSPACE / "data" / "granola-sync-state.json"
_VAULT_PATH = Path(os.environ.get("GRANOLA_VAULT_PATH", _WORKSPACE / "obsidian-vault"))
_JOB_NAME = "granola-sync"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_sync_state() -> dict[str, Any]:
    """Load sync state from JSON file, returning defaults if missing."""
    if _STATE_FILE.exists():
        try:
            with _STATE_FILE.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file %s: %s — starting fresh", _STATE_FILE, exc)
    return {"last_sync_at": None, "total_synced": 0, "last_run_at": None}


def _save_sync_state(state: dict[str, Any]) -> None:
    """Persist sync state to disk."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)
    log.debug("Saved sync state to %s", _STATE_FILE)


# ---------------------------------------------------------------------------
# write_task_output via lobster-inbox HTTP API
# ---------------------------------------------------------------------------


def _write_task_output(output: str, status: str = "success") -> None:
    """
    Write task output to Lobster's task output system.

    Tries the lobster-inbox MCP API endpoint directly. Silently skips
    if LOBSTER_INBOX_URL is not set or the call fails (non-critical).
    """
    base_url = os.environ.get("LOBSTER_INBOX_URL", "http://localhost:9922")
    url = f"{base_url}/task-output"
    payload = json.dumps({
        "job_name": _JOB_NAME,
        "output": output,
        "status": status,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.debug("write_task_output: success")
            else:
                log.debug("write_task_output: HTTP %d", resp.status)
    except (urllib.error.URLError, OSError) as exc:
        log.debug("write_task_output skipped (not available): %s", exc)


# ---------------------------------------------------------------------------
# Granola → Note detail fetching
# ---------------------------------------------------------------------------


def _fetch_notes_with_detail(
    notes_summary: list[GranolaNote],
    account_configs: list[GranolaAccountConfig],
) -> list[GranolaNote]:
    """
    For each note from list_notes() (which lacks transcript/summary),
    fetch full detail via get_note() using the correct per-account API key.

    The granola_account field from the summary note is used to look up the
    matching GranolaAccountConfig so the correct api_key is passed to get_note().
    Account attribution is preserved in the returned GranolaNote.

    Notes: The list endpoint returns id, title, owner, created_at, updated_at
    but NOT summary_markdown or transcript. We need get_note() for those.
    """
    # Build a strict lookup from account name → api_key.
    # Using dict access (not .get()) ensures an unknown account name immediately
    # raises GranolaUnknownAccountError rather than silently falling back to None,
    # which previously caused get_note() to use the primary GRANOLA_API_KEY for
    # any note whose account name was not in the config.
    api_key_by_account: dict[str, str] = {cfg.name: cfg.api_key for cfg in account_configs}

    full_notes: list[GranolaNote] = []
    for note in notes_summary:
        if note.granola_account not in api_key_by_account:
            raise GranolaUnknownAccountError(note.granola_account)
        try:
            api_key = api_key_by_account[note.granola_account]
            full = get_note(
                note.id,
                include_transcript=True,
                api_key=api_key,
                granola_account=note.granola_account,
            )
            full_notes.append(full)
            log.debug("Fetched detail for note %s (account=%s)", note.id, note.granola_account)
        except GranolaAPIError as exc:
            log.warning("Could not fetch detail for note %s: %s", note.id, exc)
            # Fall back to the summary-only version (account attribution preserved)
            full_notes.append(note)
    return full_notes


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------


def _merge_notes_deduplicated(
    notes_by_account: dict[str, list[GranolaNote]],
) -> list[GranolaNote]:
    """
    Merge notes from multiple accounts, deduplicating by note ID.

    Primary account is the authoritative source: when the same note ID appears
    in both accounts, the primary version is kept. This is a pure function.

    Args:
        notes_by_account: Mapping of account name → list of GranolaNote.

    Returns:
        Merged list with no duplicate IDs. Primary account notes appear first.
    """
    primary_notes = notes_by_account.get(ACCOUNT_PRIMARY, [])
    secondary_notes = notes_by_account.get(ACCOUNT_SECONDARY, [])

    primary_ids: set[str] = {n.id for n in primary_notes}
    secondary_unique = [n for n in secondary_notes if n.id not in primary_ids]

    return list(primary_notes) + secondary_unique


def run_sync(dry_run: bool = False) -> dict[str, Any]:
    """
    Run a full incremental sync cycle across all configured Granola accounts.

    1. Load last-sync timestamp from state file.
    2. Discover configured accounts (primary always; secondary if GRANOLA_API_KEY_2 set).
    3. Fetch all notes since last sync per account (or all on first run).
    4. Merge and deduplicate by note ID (primary wins on conflict).
    5. For each merged note, fetch full detail (transcript + summary).
    6. Write to Obsidian vault (idempotent, annotated with granola_account).
    7. Git-commit the vault.
    8. Update state file with new timestamp.
    9. Return result summary dict.

    Args:
        dry_run: If True, fetch and serialise but do not write to disk
                 or update state. Useful for testing.

    Returns:
        dict with keys: status, notes_fetched, notes_written, notes_skipped,
        notes_errored, committed, last_sync_at, vault_path, message,
        accounts_polled.
    """
    run_start = datetime.now(timezone.utc)
    state = _load_sync_state()

    last_sync_str: Optional[str] = state.get("last_sync_at")
    since: Optional[datetime] = None
    if last_sync_str:
        try:
            since = datetime.fromisoformat(last_sync_str.replace("Z", "+00:00"))
            log.info("Incremental sync since: %s", since.isoformat())
        except ValueError:
            log.warning("Could not parse last_sync_at %r — doing full sync", last_sync_str)
    else:
        log.info("No prior sync state — running full sync (all notes)")

    # Discover configured accounts
    accounts = build_account_configs_from_env()
    if not accounts:
        msg = "GRANOLA_API_KEY not set — check config.env"
        log.error(msg)
        _write_task_output(msg, status="failed")
        return {"status": "failed", "message": msg}

    account_names = [a.name for a in accounts]
    log.info("Polling %d account(s): %s", len(accounts), ", ".join(account_names))

    # Step 1: Fetch notes per account and merge
    notes_by_account: dict[str, list[GranolaNote]] = {}
    for account in accounts:
        try:
            account_notes = iter_all_notes_for_account(account, since=since)
            notes_by_account[account.name] = account_notes
            log.info("Account '%s': %d notes fetched", account.name, len(account_notes))
        except GranolaAuthError:
            msg = f"Granola authentication failed for account '{account.name}' — check API key in config.env"
            log.error(msg)
            _write_task_output(msg, status="failed")
            return {"status": "failed", "message": msg}
        except GranolaAPIError as exc:
            msg = f"Granola API error for account '{account.name}': {exc}"
            log.error(msg)
            _write_task_output(msg, status="failed")
            return {"status": "failed", "message": msg}

    notes_summary = _merge_notes_deduplicated(notes_by_account)
    log.info(
        "Merged: %s → %d total after dedup",
        ", ".join(f"{acc}={len(notes_by_account.get(acc, []))}" for acc in account_names),
        len(notes_summary),
    )

    n_fetched = len(notes_summary)
    log.info("Fetched %d notes from Granola API", n_fetched)

    if n_fetched == 0:
        msg = "No new notes since last sync."
        log.info(msg)
        # Update run timestamp even if no notes
        state["last_run_at"] = run_start.isoformat()
        if not dry_run:
            _save_sync_state(state)
        result = {
            "status": "success",
            "notes_fetched": 0,
            "notes_written": 0,
            "notes_skipped": 0,
            "notes_errored": 0,
            "committed": False,
            "last_sync_at": last_sync_str,
            "vault_path": str(_VAULT_PATH),
            "accounts_polled": account_names,
            "message": msg,
        }
        _write_task_output(json.dumps(result), status="success")
        return result

    # Step 2: Fetch full details for each note, using per-account API keys
    log.info("Fetching full detail for %d notes...", n_fetched)
    notes_full = _fetch_notes_with_detail(notes_summary, accounts)

    if dry_run:
        log.info("DRY RUN — not writing to vault")
        result = {
            "status": "dry_run",
            "notes_fetched": n_fetched,
            "notes_written": 0,
            "notes_skipped": 0,
            "notes_errored": 0,
            "committed": False,
            "vault_path": str(_VAULT_PATH),
            "accounts_polled": account_names,
            "message": f"Dry run: would write {n_fetched} notes",
        }
        return result

    # Step 3: Write to vault
    write_result: WriteResult = write_notes_batch(
        notes=notes_full,
        vault_path=_VAULT_PATH,
        commit=True,
    )

    # Step 4: Update state
    # We use the LATEST updated_at among written notes as the new cursor.
    if notes_full:
        latest_dt = max(n.updated_at for n in notes_full)
        state["last_sync_at"] = latest_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    state["last_run_at"] = run_start.isoformat()
    state["total_synced"] = state.get("total_synced", 0) + write_result.n_written
    _save_sync_state(state)

    # Step 5: Build result
    status = "failed" if write_result.n_errors > 0 and write_result.n_written == 0 else "success"
    message = (
        f"Synced {write_result.n_written} new/updated notes, "
        f"skipped {write_result.n_skipped} unchanged"
    )
    if write_result.n_errors:
        message += f", {write_result.n_errors} errors"

    result = {
        "status": status,
        "notes_fetched": n_fetched,
        "notes_written": write_result.n_written,
        "notes_skipped": write_result.n_skipped,
        "notes_errored": write_result.n_errors,
        "committed": write_result.committed,
        "last_sync_at": state["last_sync_at"],
        "vault_path": str(_VAULT_PATH),
        "accounts_polled": account_names,
        "message": message,
    }

    if write_result.errors:
        result["errors"] = [{"id": eid, "error": emsg} for eid, emsg in write_result.errors]

    output_str = json.dumps(result, indent=2)
    log.info("Sync complete: %s", message)
    _write_task_output(output_str, status=status)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run sync and print JSON result to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Load env from config files (for standalone use outside Lobster)
    _load_lobster_env()

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log.info("Running in dry-run mode")

    result = run_sync(dry_run=dry_run)
    print(json.dumps(result, indent=2))

    if result.get("status") == "failed":
        sys.exit(1)


def _load_lobster_env() -> None:
    """Load Lobster config env files if running as a standalone script."""
    config_dir = Path(os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config"))
    for env_file in [config_dir / "config.env", config_dir / "global.env"]:
        if env_file.exists():
            try:
                with env_file.open() as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, _, val = line.partition("=")
                            key = key.strip()
                            val = val.strip()
                            if key and key not in os.environ:
                                os.environ[key] = val
            except OSError as exc:
                log.warning("Could not load %s: %s", env_file, exc)


if __name__ == "__main__":
    main()
