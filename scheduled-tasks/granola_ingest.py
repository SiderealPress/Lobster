"""
Granola Ingest — Main Entry Point (Slices 1-4)

Incremental ingest of Granola meeting notes into the versioned folder
hierarchy. Designed to run every 15 minutes from cron.

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


# ---------------------------------------------------------------------------
# Bootstrap: ensure we can import sibling modules regardless of cwd
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent.resolve()
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from granola_client import GranolaClient, GranolaAPIError  # noqa: E402
from granola_state import IncrementalFetcher, StateManager  # noqa: E402
from granola_storage import NoteStorage  # noqa: E402


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
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log = _setup_logging()
    log.info("=== Granola ingest started ===")

    _load_config_env()

    # Validate API key
    api_key = os.environ.get("GRANOLA_API_KEY")
    if not api_key:
        log.error(
            "GRANOLA_API_KEY not set. Set it in ~/lobster-config/config.env "
            "or export it before running."
        )
        return 2

    try:
        client = GranolaClient(api_key=api_key)
        state_manager = StateManager(state_path=NOTES_ROOT / ".state.json")
        storage = NoteStorage(root=NOTES_ROOT)
        fetcher = IncrementalFetcher(client=client, state_manager=state_manager)
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    written = 0
    skipped = 0
    errors = 0

    try:
        for note in fetcher.fetch_new():
            try:
                path, was_new = storage.write_note(note.raw)
                if was_new:
                    written += 1
                    log.debug("Wrote note %s → %s", note.id, path)
                else:
                    skipped += 1
                    log.debug("Skipped (identical) note %s", note.id)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("Failed to write note %s: %s", note.id, exc)

    except GranolaAPIError as exc:
        log.error("Granola API error: %s", exc)
        log.info(
            "Partial run — wrote %d, skipped %d, errors %d",
            written, skipped, errors,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error during fetch: %s", exc, exc_info=True)
        return 1

    state = state_manager.load()
    cursor = state.get("cursor", "null")
    log.info(
        "Ingest complete — wrote %d new, skipped %d identical, errors %d; cursor=%s",
        written, skipped, errors, cursor,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
