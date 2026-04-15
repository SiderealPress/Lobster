"""
BIS-300: Org-Chart Enrichment Orchestration

Full enrichment pipeline:
  1. BIS-296: list_prospect_companies — fetch all prospect orgs from Kissinger
  2. BIS-297: find_supply_chain_contacts — web-search contacts per org
  3. BIS-298: dedup_crm_contacts — filter out existing CRM contacts
  4. BIS-299: add_contacts_provenance — write new contacts to CRM

Sends progress updates via the Lobster inbox send_reply MCP tool when
chat_id is configured.

Supports dry_run=True to simulate writes without side effects.

Usage:
    python org_chart_enrichment.py [--dry-run] [--chat-id <id>] \\
        [--endpoint URL] [--token TOKEN] [--search-delay N]

    # Or import and call run_enrichment() programmatically.

Returns a final summary dict:
  {
    "companies_scanned": int,
    "contacts_found": int,
    "contacts_added": int,
    "duplicates_skipped": int,
    "fuzzy_flagged": int,
    "errors": [str, ...]
  }
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Allow running as a script without installing as a package.
_BIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_BIN_DIR))

from list_prospect_companies import list_prospect_companies
from find_supply_chain_contacts import find_supply_chain_contacts
from dedup_crm_contacts import dedup_crm_contacts
from add_contacts_provenance import add_contacts_provenance

KISSINGER_ENDPOINT = os.environ.get(
    "KISSINGER_ENDPOINT", "http://localhost:8080/graphql"
)
KISSINGER_API_TOKEN = os.environ.get("KISSINGER_API_TOKEN", "")


def _send_progress(chat_id: int | str | None, text: str) -> None:
    """
    Send a progress update via mcp__lobster-inbox__send_reply if chat_id is set.

    Falls back to stderr if the MCP tool isn't available (e.g. during tests).
    """
    if not chat_id:
        print(f"[progress] {text}", file=sys.stderr)
        return

    # Try to call via lobster MCP HTTP endpoint (lobster-inbox send_reply).
    # We use the internal HTTP endpoint to avoid spawning a full Claude session.
    internal_secret = os.environ.get("LOBSTER_INTERNAL_SECRET", "")
    mcp_port = os.environ.get("LOBSTER_MCP_PORT", "9099")

    try:
        import requests as _req

        resp = _req.post(
            f"http://localhost:{mcp_port}/send_reply",
            json={"chat_id": chat_id, "text": text},
            headers={"X-Lobster-Secret": internal_secret},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        # Non-fatal — progress updates are best-effort
        print(f"[progress-send-failed] {text} | error: {exc}", file=sys.stderr)


def run_enrichment(
    *,
    dry_run: bool = False,
    chat_id: int | str | None = None,
    endpoint: str = KISSINGER_ENDPOINT,
    token: str = KISSINGER_API_TOKEN,
    search_delay: float = 1.0,
) -> dict[str, Any]:
    """
    Run the full prospect org-chart enrichment pipeline.

    Args:
        dry_run: If True, no writes are performed. Logs what would happen.
        chat_id: Lobster chat_id to send progress updates to (None = stderr only).
        endpoint: Kissinger GraphQL endpoint.
        token: Kissinger bearer token.
        search_delay: Seconds between web search requests per company.

    Returns:
        Summary dict with: companies_scanned, contacts_found, contacts_added,
        duplicates_skipped, fuzzy_flagged, errors.
    """
    summary: dict[str, Any] = {
        "companies_scanned": 0,
        "contacts_found": 0,
        "contacts_added": 0,
        "duplicates_skipped": 0,
        "fuzzy_flagged": 0,
        "errors": [],
    }
    dry_tag = " [DRY RUN]" if dry_run else ""

    # -------------------------------------------------------------------------
    # Step 1: List prospect companies
    # -------------------------------------------------------------------------
    _send_progress(
        chat_id,
        f"Prospect enrichment starting{dry_tag} — fetching prospect orgs from Kissinger...",
    )

    try:
        companies = list_prospect_companies(endpoint=endpoint, token=token)
    except Exception as exc:  # noqa: BLE001
        msg = f"FATAL: list_prospect_companies failed — {exc}"
        summary["errors"].append(msg)
        _send_progress(chat_id, f"Enrichment aborted: {msg}")
        return summary

    summary["companies_scanned"] = len(companies)

    if not companies:
        _send_progress(
            chat_id,
            "No prospect orgs found in Kissinger. "
            "Tag an org with 'prospect' to include it in enrichment.",
        )
        return summary

    _send_progress(
        chat_id,
        f"Found {len(companies)} prospect org(s). "
        f"Starting contact discovery{dry_tag}...",
    )

    # -------------------------------------------------------------------------
    # Steps 2–4: Per-company enrichment
    # -------------------------------------------------------------------------
    for idx, company in enumerate(companies, start=1):
        org_name = company["name"]
        org_id = company["id"]

        _send_progress(
            chat_id,
            f"[{idx}/{len(companies)}] Searching for contacts at {org_name}...",
        )

        # Step 2: Find contacts via web search
        try:
            contacts_raw = find_supply_chain_contacts(
                org_name, delay_secs=search_delay
            )
        except Exception as exc:  # noqa: BLE001
            err = f"find_supply_chain_contacts failed for '{org_name}': {exc}"
            summary["errors"].append(err)
            _send_progress(chat_id, f"  Warning: {err}")
            continue

        if not contacts_raw:
            _send_progress(chat_id, f"  No contacts found for {org_name} — skipping.")
            continue

        summary["contacts_found"] += len(contacts_raw)
        _send_progress(
            chat_id,
            f"  Found {len(contacts_raw)} candidate contact(s) at {org_name}. "
            f"Deduplicating against CRM...",
        )

        # Step 3: Deduplicate against CRM
        try:
            dedup_result = dedup_crm_contacts(
                contacts_raw,
                org_id=org_id,
                endpoint=endpoint,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"dedup_crm_contacts failed for '{org_name}': {exc}"
            summary["errors"].append(err)
            _send_progress(chat_id, f"  Warning: {err}")
            continue

        new_contacts = dedup_result["new"]
        duplicates = dedup_result["duplicates"]
        fuzzy = dedup_result["fuzzy_matches"]

        summary["duplicates_skipped"] += len(duplicates)
        summary["fuzzy_flagged"] += len(fuzzy)

        _send_progress(
            chat_id,
            f"  Dedup: {len(new_contacts)} new, "
            f"{len(duplicates)} duplicate(s) skipped, "
            f"{len(fuzzy)} fuzzy match(es) flagged.",
        )

        if fuzzy:
            fuzzy_names = ", ".join(c.get("name", "?") for c in fuzzy)
            _send_progress(
                chat_id,
                f"  Fuzzy matches at {org_name} — review before adding: {fuzzy_names}",
            )

        if not new_contacts:
            continue

        # Step 4: Write new contacts to CRM with provenance
        # Inject org_kissinger_id for createEdge
        for c in new_contacts:
            c["org_kissinger_id"] = org_id

        _send_progress(
            chat_id,
            f"  Writing {len(new_contacts)} new contact(s) to Kissinger{dry_tag}...",
        )

        try:
            write_results = add_contacts_provenance(
                new_contacts,
                dry_run=dry_run,
                endpoint=endpoint,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"add_contacts_provenance failed for '{org_name}': {exc}"
            summary["errors"].append(err)
            _send_progress(chat_id, f"  Warning: {err}")
            continue

        for wr in write_results:
            if wr.get("error"):
                summary["errors"].append(
                    f"{org_name}/{wr['contact'].get('name','?')}: {wr['error']}"
                )
            elif not wr.get("dry_run"):
                summary["contacts_added"] += 1

    # -------------------------------------------------------------------------
    # Final summary
    # -------------------------------------------------------------------------
    added_label = "would add" if dry_run else "added"
    summary_text = (
        f"Enrichment complete{dry_tag}:\n"
        f"  Companies scanned: {summary['companies_scanned']}\n"
        f"  Contacts found:    {summary['contacts_found']}\n"
        f"  Contacts {added_label}:   {summary['contacts_added']}\n"
        f"  Duplicates skipped: {summary['duplicates_skipped']}\n"
        f"  Fuzzy flagged:     {summary['fuzzy_flagged']}\n"
        f"  Errors:            {len(summary['errors'])}"
    )
    if summary["errors"]:
        summary_text += "\n\nErrors:\n" + "\n".join(
            f"  • {e}" for e in summary["errors"]
        )

    _send_progress(chat_id, summary_text)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run full prospect org-chart enrichment pipeline"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate without writing to CRM",
    )
    parser.add_argument(
        "--chat-id",
        default=None,
        help="Lobster chat_id for progress updates (default: stderr only)",
    )
    parser.add_argument("--endpoint", default=KISSINGER_ENDPOINT)
    parser.add_argument("--token", default=KISSINGER_API_TOKEN)
    parser.add_argument(
        "--search-delay",
        type=float,
        default=1.0,
        help="Seconds between web search requests per company (default: 1.0)",
    )
    args = parser.parse_args()

    summary = run_enrichment(
        dry_run=args.dry_run,
        chat_id=args.chat_id,
        endpoint=args.endpoint,
        token=args.token,
        search_delay=args.search_delay,
    )
    print(json.dumps(summary, indent=2))
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
