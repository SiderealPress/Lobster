"""
Granola Meeting Archive — saves all meetings as rich Markdown to
~/lobster-workspace/meetings/ with an index.json manifest.

Each meeting is saved as:
    ~/lobster-workspace/meetings/YYYY-MM-DD_HH-MM_<title-slug>.md

The index file lives at:
    ~/lobster-workspace/meetings/index.json

This script is idempotent: re-running never creates duplicate files
(deduplication is by Granola note ID recorded in index.json).

Usage:
    uv run python scheduled-tasks/granola_archive.py            # incremental (new only)
    uv run python scheduled-tasks/granola_archive.py --backfill # all meetings, ignore cursor

Exit codes:
    0 — success (including 0 new meetings)
    1 — partial failure (API or write error)
    2 — configuration error (missing API key)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Bootstrap: add src/ to sys.path so we can import integrations.granola.*
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.resolve()
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from integrations.granola.client import (  # noqa: E402
    GranolaNote,
    GranolaAPIError,
    GranolaAuthError,
    GranolaNotFoundError,
    iter_all_notes,
    get_note,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
MEETINGS_DIR = _WORKSPACE / "meetings"
INDEX_PATH = MEETINGS_DIR / "index.json"
LOG_PATH = _WORKSPACE / "logs" / "granola-archive.log"

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_config_env() -> None:
    """Load ~/lobster-config/config.env into os.environ (skip already-set keys)."""
    config_dir = Path(os.environ.get("LOBSTER_CONFIG_DIR", Path.home() / "lobster-config"))
    for env_file in [config_dir / "config.env", config_dir / "global.env"]:
        if not env_file.exists():
            continue
        with env_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging() -> logging.Logger:
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("granola_archive")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Index management (pure functions over list-of-dicts)
# ---------------------------------------------------------------------------


def _load_index() -> list[dict]:
    """Load index.json; return empty list if missing or corrupt."""
    if not INDEX_PATH.exists():
        return []
    try:
        with INDEX_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_index(index: list[dict]) -> None:
    """Atomically write index.json (sorted by date desc)."""
    sorted_index = sorted(index, key=lambda e: e.get("date", ""), reverse=True)
    tmp_path = INDEX_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(sorted_index, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, INDEX_PATH)


def _indexed_ids(index: list[dict]) -> frozenset[str]:
    """Return set of all note IDs already in the index."""
    return frozenset(entry["id"] for entry in index)


def _add_index_entry(
    index: list[dict],
    note: GranolaNote,
    filename: str,
) -> list[dict]:
    """Return a new index list with this note added (immutable update)."""
    attendees = [
        {"name": a.name, "email": a.email}
        for a in note.attendees
    ]
    entry = {
        "id": note.id,
        "title": note.title,
        "date": note.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "file": filename,
        "attendees": attendees,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }
    return [*index, entry]


# ---------------------------------------------------------------------------
# Filename and markdown generation
# ---------------------------------------------------------------------------

# Maximum title slug length in filename
_SLUG_MAX_LEN = 50


def _slugify(text: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Convert a title to a safe filename slug."""
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        truncated = s[:max_len].rsplit("-", 1)[0]
        s = truncated if truncated else s[:max_len]
    return s or "untitled"


def _meeting_datetime(note: GranolaNote) -> datetime:
    """Return the best available meeting start time (ET is stored as-is in UTC)."""
    if note.calendar_event and note.calendar_event.scheduled_start_time:
        return note.calendar_event.scheduled_start_time.astimezone(timezone.utc)
    return note.created_at.astimezone(timezone.utc)


def _build_filename(note: GranolaNote) -> str:
    """Build YYYY-MM-DD_HH-MM_<slug>.md filename."""
    dt = _meeting_datetime(note)
    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H-%M")
    slug = _slugify(note.title)
    return f"{date_str}_{time_str}_{slug}.md"


def _duration_minutes(note: GranolaNote) -> Optional[int]:
    """Return meeting duration in minutes, or None."""
    if note.calendar_event:
        start = note.calendar_event.scheduled_start_time
        end = note.calendar_event.scheduled_end_time
        if start and end:
            return max(0, int((end - start).total_seconds() / 60))
    return None


def _yaml_str(value: str) -> str:
    """Wrap a string in double quotes, escaping internal quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_dt_yaml(dt: Optional[datetime]) -> str:
    if dt is None:
        return '""'
    return _yaml_str(dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _note_to_markdown(note: GranolaNote) -> str:
    """
    Serialize a GranolaNote to the meetings archive Markdown format.

    Pure function — no I/O.
    """
    dt = _meeting_datetime(note)

    # --- Attendees YAML ---
    attendees_lines: list[str] = []
    for a in note.attendees:
        name_q = _yaml_str(a.name) if a.name else '""'
        email_q = _yaml_str(a.email) if a.email else '""'
        attendees_lines.append(f"  - name: {name_q}")
        attendees_lines.append(f"    email: {email_q}")
    attendees_yaml = "\n".join(attendees_lines) if attendees_lines else "  []"

    # --- Duration ---
    duration_min = _duration_minutes(note)
    duration_str = str(duration_min) if duration_min is not None else "null"

    # --- Granola web URL ---
    # The GranolaNote dataclass from the existing client does not expose web_url,
    # so we leave this blank. It can be enriched in a future slice.
    granola_url_yaml = '""'

    # --- Frontmatter ---
    frontmatter_lines = [
        "---",
        f"id: {note.id}",
        f"title: {_yaml_str(note.title)}",
        f"date: {dt.strftime('%Y-%m-%d')}",
        f"time: {dt.strftime('%H:%M')} UTC",
        f"duration_minutes: {duration_str}",
        "attendees:",
        attendees_yaml,
        f"participants_count: {len(note.attendees)}",
        f"granola_url: {granola_url_yaml}",
        f"archived_at: {datetime.now(timezone.utc).isoformat()}",
        "source: granola",
        "---",
    ]
    frontmatter = "\n".join(frontmatter_lines)

    # --- Body ---
    title_line = f"# {note.title}"

    if note.summary_markdown:
        summary = f"## Summary\n\n{note.summary_markdown.strip()}"
    elif note.summary_text:
        summary = f"## Summary\n\n{note.summary_text.strip()}"
    else:
        summary = "## Summary\n\n_No summary available._"

    # Transcript / Notes
    if note.transcript:
        segments: list[str] = []
        current_speaker: Optional[str] = None
        for seg in note.transcript:
            speaker = seg.speaker or "Unknown"
            if speaker != current_speaker:
                if segments:
                    segments.append("")
                segments.append(f"**{speaker}**")
                current_speaker = speaker
            text = seg.text.strip()
            if text:
                segments.append(text)
        transcript_body = "\n".join(segments)
        notes_section = f"## Transcript / Notes\n\n{transcript_body}"
    else:
        notes_section = "## Transcript / Notes\n\n_No transcript available._"

    body = "\n\n".join([title_line, summary, notes_section])
    return frontmatter + "\n\n" + body + "\n"


# ---------------------------------------------------------------------------
# Per-note archive write (idempotent, functional)
# ---------------------------------------------------------------------------


def _archive_note(
    note: GranolaNote,
    index: list[dict],
    log: logging.Logger,
) -> tuple[bool, list[dict], str]:
    """
    Archive a single note.

    Returns (was_written, updated_index, filename).
    Does not write index — caller is responsible for persisting.
    Pure with respect to the index list; I/O only for the markdown file.
    """
    filename = _build_filename(note)
    dest = MEETINGS_DIR / filename

    content = _note_to_markdown(note)

    if dest.exists():
        existing = dest.read_text(encoding="utf-8")
        if existing == content:
            log.debug("Skipped (identical): %s → %s", note.id, filename)
            return False, index, filename
        # Content changed — overwrite (meeting may have been updated in Granola)
        log.debug("Updated: %s → %s", note.id, filename)
    else:
        log.debug("New: %s → %s", note.id, filename)

    dest.write_text(content, encoding="utf-8")

    # Update index (remove stale entry if it exists, then add fresh one)
    updated_index = [e for e in index if e.get("id") != note.id]
    updated_index = _add_index_entry(updated_index, note, filename)
    return True, updated_index, filename


# ---------------------------------------------------------------------------
# Fetch full note details
# ---------------------------------------------------------------------------


def _fetch_full_note(note: GranolaNote, log: logging.Logger) -> Optional[GranolaNote]:
    """
    Fetch note with transcript via get_note(). Falls back to summary-only
    on 404 (note not yet summarised). Returns None on hard API errors.
    """
    try:
        return get_note(note.id, include_transcript=True)
    except GranolaNotFoundError:
        log.warning("Note %s not found / not yet summarised — skipping", note.id)
        return None
    except GranolaAPIError as exc:
        log.warning("API error fetching note %s: %s", note.id, exc)
        return None


# ---------------------------------------------------------------------------
# Main archiver
# ---------------------------------------------------------------------------


def _run_archive(backfill: bool, log: logging.Logger) -> int:
    """
    Main archive loop. Returns exit code.

    backfill=True  — fetch all notes from the API (ignores index for filtering,
                     but still skips identical files on disk).
    backfill=False — only fetch notes not already in index.json.
    """
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("GRANOLA_API_KEY", "").strip()
    if not api_key:
        log.error("GRANOLA_API_KEY not set. Set it in ~/lobster-config/config.env.")
        return 2

    index = _load_index()
    known_ids = _indexed_ids(index)

    log.info(
        "=== Granola archive started (mode=%s, indexed=%d) ===",
        "backfill" if backfill else "incremental",
        len(known_ids),
    )

    # Step 1: List all notes from API
    try:
        notes_summary = iter_all_notes()
    except GranolaAuthError:
        log.error("Granola authentication failed — check GRANOLA_API_KEY")
        return 2
    except GranolaAPIError as exc:
        log.error("Granola API error during list: %s", exc)
        return 1

    log.info("API returned %d notes total", len(notes_summary))

    # Step 2: Filter to only new notes (unless backfill)
    if backfill:
        to_process = notes_summary
        log.info("Backfill mode: processing all %d notes", len(to_process))
    else:
        to_process = [n for n in notes_summary if n.id not in known_ids]
        log.info("Incremental mode: %d new notes to archive", len(to_process))

    if not to_process:
        log.info("Nothing to do.")
        return 0

    # Step 3: For each note, fetch full detail and archive
    written = 0
    skipped = 0
    errors = 0

    for note_summary in to_process:
        full_note = _fetch_full_note(note_summary, log)
        if full_note is None:
            errors += 1
            continue

        try:
            was_written, index, filename = _archive_note(full_note, index, log)
            if was_written:
                written += 1
                log.info("Archived: %s → %s", full_note.id, filename)
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning("Failed to archive note %s: %s", note_summary.id, exc)

        # Persist index after each note so a crash doesn't lose progress
        _save_index(index)

    log.info(
        "=== Archive complete: written=%d skipped=%d errors=%d ===",
        written, skipped, errors,
    )
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive Granola meetings to ~/lobster-workspace/meetings/",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process all meetings from the API (not just new ones).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    log = _setup_logging()
    _load_config_env()
    return _run_archive(backfill=args.backfill, log=log)


if __name__ == "__main__":
    sys.exit(main())
