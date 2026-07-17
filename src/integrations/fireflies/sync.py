"""
Fireflies → Obsidian incremental sync.

Entry point for the scheduled job. Reads the last-sync timestamp from a
state file, fetches only transcripts created since then (or all transcripts
on first run) for every configured Fireflies account, writes to the Obsidian
vault, git-commits, and updates state. Mirrors integrations.granola.sync.

Unlike Granola's sync (which merges a hardcoded 'primary'/'secondary' pair),
this module merges an arbitrary, dynamically-discovered set of accounts —
see build_account_configs_from_env() in client.py. Adding a new teammate's
FIREFLIES_API_KEY_<NAME> env var is picked up automatically by both account
discovery and this merge step, with no code change required.

State file: ~/lobster-workspace/data/fireflies-sync-state.json
Vault path: ~/lobster-workspace/obsidian-vault/

Usage (standalone):
    cd ~/lobster
    uv run python -m integrations.fireflies.sync

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

_SRC_DIR = Path(__file__).parent.parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from integrations.fireflies.client import (
    FirefliesAccountConfig,
    FirefliesAPIError,
    FirefliesAuthError,
    FirefliesTranscript,
    FirefliesUnknownAccountError,
    build_account_configs_from_env,
    get_transcript,
    iter_all_transcripts_for_account,
)
from integrations.fireflies.vault_writer import WriteResult, write_transcripts_batch

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
_STATE_FILE = _WORKSPACE / "data" / "fireflies-sync-state.json"
_VAULT_PATH = Path(os.environ.get("FIREFLIES_VAULT_PATH", _WORKSPACE / "obsidian-vault"))
_JOB_NAME = "fireflies-sync"


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
# Fireflies → transcript detail fetching
# ---------------------------------------------------------------------------


def _fetch_transcripts_with_detail(
    transcripts_summary: list[FirefliesTranscript],
    account_configs: list[FirefliesAccountConfig],
) -> list[FirefliesTranscript]:
    """
    For each transcript from iter_all_transcripts_for_account() (summary
    fields only), fetch full detail (summary/action_items/sentences) via
    get_transcript() using the correct per-account API key.

    The fireflies_account field from the summary transcript is used to look
    up the matching FirefliesAccountConfig, so the correct api_key is passed
    to get_transcript(). Using dict access (not .get()) means an unknown
    account name raises FirefliesUnknownAccountError immediately rather than
    silently falling back to the primary API key — this is the same
    regression class the Granola integration guards against.
    """
    api_key_by_account: dict[str, str] = {cfg.name: cfg.api_key for cfg in account_configs}

    full_transcripts: list[FirefliesTranscript] = []
    for transcript in transcripts_summary:
        if transcript.fireflies_account not in api_key_by_account:
            raise FirefliesUnknownAccountError(transcript.fireflies_account)
        try:
            api_key = api_key_by_account[transcript.fireflies_account]
            full = get_transcript(
                transcript.id,
                api_key=api_key,
                fireflies_account=transcript.fireflies_account,
            )
            full_transcripts.append(full)
            log.debug(
                "Fetched detail for transcript %s (account=%s)",
                transcript.id, transcript.fireflies_account,
            )
        except FirefliesAPIError as exc:
            log.warning("Could not fetch detail for transcript %s: %s", transcript.id, exc)
            full_transcripts.append(transcript)
    return full_transcripts


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------


def _merge_transcripts_deduplicated(
    transcripts_by_account: dict[str, list[FirefliesTranscript]],
) -> list[FirefliesTranscript]:
    """
    Merge transcripts from an arbitrary set of accounts, deduplicating by ID.

    Unlike Granola's merge (hardcoded to exactly 'primary' and 'secondary'),
    this handles however many named accounts build_account_configs_from_env()
    discovered. The primary account is authoritative: if the same transcript
    ID appears under multiple accounts, the primary version wins; among
    non-primary accounts, dict iteration order determines which is kept
    (a transcript genuinely shared across two teammates' non-primary
    accounts is a rare edge case, not the common path this optimises for).

    This is a pure function — no I/O, no mutation of inputs.
    """
    seen_ids: set[str] = set()
    merged: list[FirefliesTranscript] = []

    # Primary first (authoritative), then every other account in whatever
    # order the caller provided (dict iteration order).
    ordered_names = [name for name in transcripts_by_account if name == "primary"]
    ordered_names += [name for name in transcripts_by_account if name != "primary"]

    for name in ordered_names:
        for transcript in transcripts_by_account[name]:
            if transcript.id in seen_ids:
                continue
            seen_ids.add(transcript.id)
            merged.append(transcript)

    return merged


def run_sync(dry_run: bool = False) -> dict[str, Any]:
    """
    Run a full incremental sync cycle across all configured Fireflies accounts.

    1. Load last-sync timestamp from state file.
    2. Discover configured accounts (primary + any FIREFLIES_API_KEY_<NAME>).
    3. Fetch all transcripts since last sync per account (or all on first run).
    4. Merge and deduplicate by transcript ID (primary wins on conflict).
    5. For each merged transcript, fetch full detail (summary + sentences).
    6. Write to Obsidian vault (idempotent, annotated with fireflies_account).
    7. Git-commit the vault.
    8. Update state file with new timestamp.
    9. Return result summary dict.

    Args:
        dry_run: If True, fetch and serialise but do not write to disk
                 or update state. Useful for testing.
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
        log.info("No prior sync state — running full sync (all transcripts)")

    accounts = build_account_configs_from_env()
    if not accounts:
        msg = "FIREFLIES_API_KEY not set — check config.env"
        log.error(msg)
        _write_task_output(msg, status="failed")
        return {"status": "failed", "message": msg}

    account_names = [a.name for a in accounts]
    log.info("Polling %d Fireflies account(s): %s", len(accounts), ", ".join(account_names))

    transcripts_by_account: dict[str, list[FirefliesTranscript]] = {}
    for account in accounts:
        try:
            account_transcripts = iter_all_transcripts_for_account(account, since=since)
            transcripts_by_account[account.name] = account_transcripts
            log.info("Account '%s': %d transcripts fetched", account.name, len(account_transcripts))
        except FirefliesAuthError:
            msg = f"Fireflies authentication failed for account '{account.name}' — check API key in config.env"
            log.error(msg)
            _write_task_output(msg, status="failed")
            return {"status": "failed", "message": msg}
        except FirefliesAPIError as exc:
            msg = f"Fireflies API error for account '{account.name}': {exc}"
            log.error(msg)
            _write_task_output(msg, status="failed")
            return {"status": "failed", "message": msg}

    transcripts_summary = _merge_transcripts_deduplicated(transcripts_by_account)
    n_fetched = len(transcripts_summary)
    log.info(
        "Merged: %s → %d total after dedup",
        ", ".join(f"{acc}={len(transcripts_by_account.get(acc, []))}" for acc in account_names),
        n_fetched,
    )

    if n_fetched == 0:
        msg = "No new transcripts since last sync."
        log.info(msg)
        state["last_run_at"] = run_start.isoformat()
        if not dry_run:
            _save_sync_state(state)
        result = {
            "status": "success",
            "transcripts_fetched": 0,
            "transcripts_written": 0,
            "transcripts_skipped": 0,
            "transcripts_errored": 0,
            "committed": False,
            "last_sync_at": last_sync_str,
            "vault_path": str(_VAULT_PATH),
            "accounts_polled": account_names,
            "message": msg,
        }
        _write_task_output(json.dumps(result), status="success")
        return result

    log.info("Fetching full detail for %d transcripts...", n_fetched)
    transcripts_full = _fetch_transcripts_with_detail(transcripts_summary, accounts)

    if dry_run:
        log.info("DRY RUN — not writing to vault")
        return {
            "status": "dry_run",
            "transcripts_fetched": n_fetched,
            "transcripts_written": 0,
            "transcripts_skipped": 0,
            "transcripts_errored": 0,
            "committed": False,
            "vault_path": str(_VAULT_PATH),
            "accounts_polled": account_names,
            "message": f"Dry run: would write {n_fetched} transcripts",
        }

    write_result: WriteResult = write_transcripts_batch(
        transcripts=transcripts_full,
        vault_path=_VAULT_PATH,
        commit=True,
    )

    if transcripts_full:
        dated = [t.date for t in transcripts_full if t.date is not None]
        if dated:
            latest_dt = max(dated)
            state["last_sync_at"] = latest_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    state["last_run_at"] = run_start.isoformat()
    state["total_synced"] = state.get("total_synced", 0) + write_result.n_written
    _save_sync_state(state)

    status = "failed" if write_result.n_errors > 0 and write_result.n_written == 0 else "success"
    message = (
        f"Synced {write_result.n_written} new/updated transcripts, "
        f"skipped {write_result.n_skipped} unchanged"
    )
    if write_result.n_errors:
        message += f", {write_result.n_errors} errors"

    result = {
        "status": status,
        "transcripts_fetched": n_fetched,
        "transcripts_written": write_result.n_written,
        "transcripts_skipped": write_result.n_skipped,
        "transcripts_errored": write_result.n_errors,
        "committed": write_result.committed,
        "last_sync_at": state.get("last_sync_at"),
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
