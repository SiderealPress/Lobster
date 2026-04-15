"""
Granola Backfill — Slice 5

One-time (re-runnable) script to import all historical Granola notes.
Uses the same NoteStorage layer (idempotent writes), so re-running
safely skips already-present notes.

Does NOT touch the incremental state file — backfill is decoupled
from the regular ingest cursor.

Usage:
    uv run granola_backfill.py
    uv run granola_backfill.py --since 2025-01-01
    uv run granola_backfill.py --dry-run
    uv run granola_backfill.py --since 2025-06-01 --dry-run

Exit codes:
  0 — success
  1 — partial failure (some writes failed)
  2 — configuration error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent.resolve()
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from granola_client import GranolaClient, GranolaAPIError  # noqa: E402
from granola_storage import NoteStorage  # noqa: E402


NOTES_ROOT = Path.home() / "lobster-workspace" / "granola-notes"
PROGRESS_INTERVAL = 50  # print progress every N notes


# ---------------------------------------------------------------------------
# Load config.env
# ---------------------------------------------------------------------------

def _load_config_env() -> None:
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
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill all historical Granola notes into lobster-workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only import notes created on or after this date (UTC).",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without writing anything.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    log = logging.getLogger("granola_backfill")

    _load_config_env()

    api_key = os.environ.get("GRANOLA_API_KEY")
    if not api_key:
        log.error("GRANOLA_API_KEY not set. Set it in ~/lobster-config/config.env.")
        return 2

    # Parse --since date into ISO timestamp
    created_after: str | None = None
    if args.since:
        try:
            d = date.fromisoformat(args.since)
            created_after = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
        except ValueError:
            log.error("--since must be in YYYY-MM-DD format, got: %s", args.since)
            return 2

    if args.dry_run:
        log.info("DRY RUN — no files will be written")

    client = GranolaClient(api_key=api_key)
    storage = NoteStorage(root=NOTES_ROOT)

    written = 0
    skipped = 0
    errors = 0
    total_seen = 0
    cursor: str | None = None

    log.info(
        "Starting backfill%s%s",
        f" since {args.since}" if args.since else " (all time)",
        " [dry-run]" if args.dry_run else "",
    )

    try:
        while True:
            result = client.list_notes(created_after=created_after, cursor=cursor)

            if not result.notes:
                break

            for note in result.notes:
                total_seen += 1

                if args.dry_run:
                    # Check if it would be a new write
                    exists = storage.note_exists(
                        note.id, created_at=note.created_at
                    )
                    if exists:
                        skipped += 1
                        log.debug("[dry-run] SKIP %s (already present)", note.id)
                    else:
                        written += 1
                        log.debug("[dry-run] WOULD WRITE %s (%s)", note.id, note.title)
                else:
                    try:
                        path, was_new = storage.write_note(note.raw)
                        if was_new:
                            written += 1
                            log.debug("Wrote %s → %s", note.id, path)
                        else:
                            skipped += 1
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        log.warning("Failed to write %s: %s", note.id, exc)

                if total_seen % PROGRESS_INTERVAL == 0:
                    log.info(
                        "Progress: %d notes processed (written=%d, skipped=%d, errors=%d)",
                        total_seen, written, skipped, errors,
                    )

            if not result.has_more:
                break
            cursor = result.cursor

    except GranolaAPIError as exc:
        log.error("API error during backfill: %s", exc)
        log.info("Partial — written=%d, skipped=%d, errors=%d", written, skipped, errors)
        return 1

    log.info(
        "Backfill complete%s: %d notes written, %d skipped (already present)%s",
        " [dry-run]" if args.dry_run else "",
        written,
        skipped,
        f", {errors} errors" if errors else "",
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
