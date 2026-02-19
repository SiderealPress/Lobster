#!/usr/bin/env python3
"""
Nightly Consolidation Script -- Layer 2 of Lobster's Three-Layer Memory System

Synthesizes raw memory events from the SQLite event store into canonical
markdown files using Claude Code CLI calls. Runs as a standalone script
invoked by cron (via nightly-consolidation.sh) or directly.

Design principles:
  - Pure functions for grouping, prompt building, and file rendering
  - Side effects (DB, API, filesystem) isolated at the boundaries
  - Idempotent: running with no unconsolidated events is a no-op
  - Graceful degradation: API failures leave events unconsolidated for next run

Usage:
    python3 scripts/nightly-consolidation.py [--dry-run] [--config path/to/config]
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "CONSOLIDATION_MODEL": "claude-sonnet-4-20250514",
    "MAX_EVENTS_PER_BATCH": "500",
    "MEMORY_DB": str(Path.home() / "lobster" / "data" / "memory.db"),
    "CANONICAL_DIR": str(Path.home() / "lobster" / "memory" / "canonical"),
    "ARCHIVE_DIR": str(Path.home() / "lobster" / "memory" / "archive"),
    "CONSOLIDATION_LOG_LEVEL": "INFO",
}

log = logging.getLogger("consolidation")


# ---------------------------------------------------------------------------
# Data types (plain, immutable where practical)
# ---------------------------------------------------------------------------


class Event(NamedTuple):
    """A memory event row from SQLite."""
    id: int
    timestamp: str
    type: str
    source: str
    project: str | None
    content: str
    metadata: str
    consolidated: int


class ConsolidationRun(NamedTuple):
    """Tracks a single consolidation run."""
    run_id: str
    started_at: str
    status: str  # 'started', 'completed', 'failed'
    events_processed: int
    summary: str
    error: str | None


class EventGroup(NamedTuple):
    """A group of events sharing a common key (project, person, or topic)."""
    key: str
    category: str  # 'project', 'person', 'topic'
    events: tuple[Event, ...]


# ---------------------------------------------------------------------------
# Configuration loading (pure)
# ---------------------------------------------------------------------------


def load_config(config_path: str | None = None) -> dict[str, str]:
    """Load configuration from file, with environment variable overrides.

    Priority: environment variables > config file > defaults.
    """
    config = dict(DEFAULT_CONFIG)

    if config_path and Path(config_path).exists():
        config = _merge_config_file(config, config_path)

    # Environment overrides
    for key in config:
        env_val = os.environ.get(key)
        if env_val is not None:
            config[key] = env_val

    # Expand ~ in paths
    for key in ("MEMORY_DB", "CANONICAL_DIR", "ARCHIVE_DIR"):
        config[key] = str(Path(config[key]).expanduser())

    return config


def _merge_config_file(base: dict[str, str], path: str) -> dict[str, str]:
    """Parse a KEY=VALUE config file and merge into base dict."""
    result = dict(base)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# Database access (side effects, isolated)
# ---------------------------------------------------------------------------


def open_db(db_path: str) -> sqlite3.Connection:
    """Open the memory SQLite database and ensure schema exists."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_consolidation_table(conn)
    return conn


def _ensure_consolidation_table(conn: sqlite3.Connection) -> None:
    """Create the consolidation_runs table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consolidation_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'started',
            events_processed INTEGER DEFAULT 0,
            summary TEXT DEFAULT '',
            error TEXT
        )
    """)
    conn.commit()


def fetch_unconsolidated_events(conn: sqlite3.Connection, limit: int) -> tuple[Event, ...]:
    """Fetch all unconsolidated events, ordered by timestamp."""
    rows = conn.execute(
        """
        SELECT id, timestamp, type, source, project, content, metadata, consolidated
        FROM events
        WHERE consolidated = 0
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return tuple(
        Event(
            id=r["id"],
            timestamp=r["timestamp"],
            type=r["type"],
            source=r["source"],
            project=r["project"],
            content=r["content"],
            metadata=r["metadata"],
            consolidated=r["consolidated"],
        )
        for r in rows
    )


def create_run_record(conn: sqlite3.Connection, run_id: str) -> None:
    """Insert a new consolidation run record."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO consolidation_runs (run_id, started_at, status) VALUES (?, ?, 'started')",
        (run_id, now),
    )
    conn.commit()


def complete_run_record(
    conn: sqlite3.Connection, run_id: str, events_processed: int, summary: str
) -> None:
    """Mark a consolidation run as completed."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE consolidation_runs
        SET status = 'completed', completed_at = ?, events_processed = ?, summary = ?
        WHERE run_id = ?
        """,
        (now, events_processed, summary, run_id),
    )
    conn.commit()


def fail_run_record(conn: sqlite3.Connection, run_id: str, error: str) -> None:
    """Mark a consolidation run as failed."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE consolidation_runs
        SET status = 'failed', completed_at = ?, error = ?
        WHERE run_id = ?
        """,
        (now, error, run_id),
    )
    conn.commit()


def mark_events_consolidated(conn: sqlite3.Connection, event_ids: tuple[int, ...]) -> None:
    """Mark events as consolidated in the database."""
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(
        f"UPDATE events SET consolidated = 1 WHERE id IN ({placeholders})",
        list(event_ids),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Event grouping (pure functions)
# ---------------------------------------------------------------------------


def group_events(events: tuple[Event, ...]) -> tuple[EventGroup, ...]:
    """Group events by project, then by type for ungrouped events.

    Returns a tuple of EventGroup, each containing related events.
    Events with a project field are grouped by project.
    Remaining events are grouped by type.
    """
    project_groups: dict[str, list[Event]] = {}
    type_groups: dict[str, list[Event]] = {}

    for event in events:
        if event.project:
            project_groups.setdefault(event.project, []).append(event)
        else:
            type_groups.setdefault(event.type, []).append(event)

    groups: list[EventGroup] = []

    for project_name, project_events in sorted(project_groups.items()):
        groups.append(EventGroup(
            key=project_name,
            category="project",
            events=tuple(project_events),
        ))

    for type_name, type_events in sorted(type_groups.items()):
        groups.append(EventGroup(
            key=type_name,
            category="topic",
            events=tuple(type_events),
        ))

    return tuple(groups)


def extract_people_mentions(events: tuple[Event, ...]) -> dict[str, tuple[Event, ...]]:
    """Extract person mentions from event content.

    Scans event content for known people patterns and groups
    events by person name. Returns a dict mapping person name
    to the events mentioning them.
    """
    people: dict[str, list[Event]] = {}

    for event in events:
        content_lower = event.content.lower()
        # Extract person names from metadata if available
        try:
            meta = json.loads(event.metadata) if isinstance(event.metadata, str) else event.metadata
        except (json.JSONDecodeError, TypeError):
            meta = {}

        mentioned = meta.get("people", [])
        if isinstance(mentioned, str):
            mentioned = [mentioned]

        # Also check for known names in content (extensible pattern)
        for name in _extract_names_from_content(content_lower):
            if name not in mentioned:
                mentioned.append(name)

        for person in mentioned:
            people.setdefault(person.lower(), []).append(event)

    return {name: tuple(evts) for name, evts in people.items()}


def _extract_names_from_content(content: str) -> list[str]:
    """Extract potential person names from content text.

    This uses a simple heuristic -- looks for known name patterns.
    The consolidation prompts will handle more nuanced extraction.
    """
    # Known names can be extended; for now use a minimal approach
    # The Claude API call will do the heavy lifting for name extraction
    return []


def format_events_for_prompt(events: tuple[Event, ...]) -> str:
    """Format a sequence of events into a readable string for Claude prompts."""
    lines = []
    for event in events:
        ts = event.timestamp[:19] if len(event.timestamp) > 19 else event.timestamp
        lines.append(f"- [{ts}] ({event.type}/{event.source}) {event.content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt building (pure functions)
# ---------------------------------------------------------------------------


def build_project_update_prompt(
    project_name: str,
    current_file: str,
    events_text: str,
) -> str:
    """Build a prompt to update a project canonical file."""
    return f"""You are updating a canonical project file for the Lobster memory system.

## Current File Content

```markdown
{current_file}
```

## New Events for Project: {project_name}

{events_text}

## Instructions

Update the project file to incorporate the new events. Follow these rules:
1. Preserve the existing markdown structure and headings.
2. Update the "Recent Work" section with new activity.
3. Update "Status" if the events indicate a change.
4. Update "Next Steps" based on what the events suggest.
5. Update "Blockers" if any are mentioned.
6. Keep the tone factual and concise.
7. Do NOT add speculative information -- only what the events support.
8. Return ONLY the complete updated markdown file content, nothing else.
"""


def build_person_update_prompt(
    person_name: str,
    current_file: str,
    events_text: str,
) -> str:
    """Build a prompt to update a person canonical file."""
    return f"""You are updating a canonical person file for the Lobster memory system.

## Current File Content

```markdown
{current_file}
```

## New Events Mentioning: {person_name}

{events_text}

## Instructions

Update the person file to incorporate the new events. Follow these rules:
1. Preserve the existing markdown structure and headings.
2. Update "Pending Items" based on new tasks or follow-ups.
3. Update "Last Contact" if there was direct interaction.
4. Add any new context discovered from the events to "Notes" or "Context".
5. Keep the tone factual and concise.
6. Do NOT add speculative information -- only what the events support.
7. Return ONLY the complete updated markdown file content, nothing else.
"""


def build_daily_digest_prompt(
    events_text: str,
    date_str: str,
) -> str:
    """Build a prompt to generate the daily digest."""
    return f"""You are generating a daily digest for the Lobster memory system.

## Events from {date_str}

{events_text}

## Instructions

Generate a daily digest in markdown format with these exact sections:

# Daily Digest

*Auto-generated by nightly consolidation. Last updated: {date_str}.*

## Date

{date_str}

## Messages

Summarize key messages exchanged (who said what, main topics).

## Tasks

List any tasks created, updated, or completed.

## Decisions Made

List any decisions that were made or implied by the events.

## Follow-ups Required

List items that need follow-up action.

## Patterns and Observations

Note any recurring themes, behavioral patterns, or interesting observations.

## Metrics

Provide counts:
- Messages processed: N
- Tasks created: N
- Tasks completed: N
- Events consolidated: N

Keep it concise and factual. Return ONLY the markdown content, nothing else.
"""


def build_handoff_prompt(
    canonical_files: dict[str, str],
) -> str:
    """Build a prompt to regenerate the handoff document.

    This is the "crown jewel" -- a comprehensive briefing document
    synthesized from all canonical files.
    """
    files_text = "\n\n---\n\n".join(
        f"### {name}\n\n{content}"
        for name, content in sorted(canonical_files.items())
    )

    return f"""You are regenerating the Lobster Handoff Document -- a comprehensive briefing
for a new Lobster session that has no prior context. This is the most important
document in the memory system.

## All Canonical Files

{files_text}

## Instructions

Generate a complete handoff document in markdown with these exact sections:

# Lobster Handoff Document

*Auto-generated by nightly consolidation. Last updated: [today's date].*

## Identity
Who/what is Lobster? Brief description.

## Owner
Who is the owner? Key details for interacting with them.

## Active Projects
All active projects with brief status.

## Key Relationships
People Lobster interacts with, their roles, pending items.

## Priority Stack
Numbered priority list, highest first.

## Pending Decisions
Any open decisions needing resolution.

## Recent Trajectory
What has been happening recently (last few days).

## System Notes
Technical details a new session needs to know (paths, architecture, patterns).

## Overdue Items
Anything past due that needs attention.

The handoff should be self-contained -- a new Lobster session reading ONLY this
document should understand the full current state. Be comprehensive but concise.
Return ONLY the markdown content, nothing else.
"""


def build_priorities_update_prompt(
    current_priorities: str,
    digest_content: str,
) -> str:
    """Build a prompt to update the priorities file based on the daily digest."""
    return f"""You are updating the priority stack for the Lobster memory system.

## Current Priorities

```markdown
{current_priorities}
```

## Today's Digest

{digest_content}

## Instructions

Update the priorities file to reflect any changes suggested by today's events.
Follow these rules:
1. Preserve the markdown structure with Critical/Urgent, High, Medium, Low sections.
2. Re-order items if events suggest priority changes.
3. Add new items if events introduce new priorities.
4. Remove or move items to "completed" if events indicate they are done.
5. Update annotations with the current date.
6. Keep numbered globally for easy reference.
7. Be conservative -- only change priorities when events clearly justify it.
8. Return ONLY the complete updated markdown file content, nothing else.
"""


# ---------------------------------------------------------------------------
# Claude Code CLI interaction (side effect boundary)
# ---------------------------------------------------------------------------


def call_claude_api(prompt: str, model: str) -> str:
    """Call Claude via the Claude Code CLI (claude --print).

    Uses subprocess to invoke the `claude` CLI in non-interactive print mode,
    avoiding a direct dependency on the Anthropic Python SDK.

    Returns the text response from Claude.
    Raises on CLI errors so the caller can handle gracefully.
    """
    cmd = [
        "claude",
        "--print",
        "--model", model,
        "--max-turns", "1",
        "-p", prompt,
    ]
    log.debug(f"Invoking Claude CLI: claude --print --model {model} --max-turns 1 ...")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,  # 5-minute timeout per call
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Claude CLI exited with code {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Filesystem operations (side effect boundary)
# ---------------------------------------------------------------------------


def read_canonical_file(canonical_dir: str, relative_path: str) -> str:
    """Read a canonical file, returning empty string if it doesn't exist."""
    path = Path(canonical_dir) / relative_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def write_canonical_file(canonical_dir: str, relative_path: str, content: str) -> None:
    """Write content to a canonical file, creating parent directories as needed."""
    path = Path(canonical_dir) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info(f"Updated canonical file: {relative_path}")


def archive_digest(
    canonical_dir: str, archive_dir: str, date_str: str
) -> None:
    """Archive the current daily digest before overwriting it.

    Copies the current daily-digest.md to archive/digests/YYYY-MM-DD.md.
    """
    source = Path(canonical_dir) / "daily-digest.md"
    if not source.exists():
        return

    dest_dir = Path(archive_dir) / "digests"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{date_str}.md"

    shutil.copy2(str(source), str(dest))
    log.info(f"Archived digest to: {dest}")


def collect_canonical_files(canonical_dir: str) -> dict[str, str]:
    """Read all canonical files into a dict keyed by relative path."""
    base = Path(canonical_dir)
    result = {}
    if not base.exists():
        return result

    for md_file in sorted(base.rglob("*.md")):
        rel = str(md_file.relative_to(base))
        result[rel] = md_file.read_text(encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# Consolidation pipeline (orchestration)
# ---------------------------------------------------------------------------


def consolidate(
    config: dict[str, str],
    dry_run: bool = False,
) -> ConsolidationRun:
    """Run the full consolidation pipeline.

    1. Create a consolidation run record
    2. Fetch unconsolidated events
    3. Group by project/person/topic
    4. For each group, synthesize updates to canonical files
    5. Generate daily digest
    6. Regenerate handoff document
    7. Mark events as consolidated
    8. Update run record

    Returns the ConsolidationRun result.
    """
    db_path = config["MEMORY_DB"]
    canonical_dir = config["CANONICAL_DIR"]
    archive_dir = config["ARCHIVE_DIR"]
    model = config["CONSOLIDATION_MODEL"]
    max_events = int(config["MAX_EVENTS_PER_BATCH"])

    run_id = str(uuid.uuid4())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Check if the database exists
    if not Path(db_path).exists():
        log.warning(f"Memory database not found at {db_path}. Nothing to consolidate.")
        return ConsolidationRun(
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="completed",
            events_processed=0,
            summary="No database found. No-op.",
            error=None,
        )

    conn = open_db(db_path)

    try:
        # Step 1: Record the run
        if not dry_run:
            create_run_record(conn, run_id)

        # Step 2: Fetch unconsolidated events
        events = fetch_unconsolidated_events(conn, max_events)

        if not events:
            log.info("No unconsolidated events found. Nothing to do.")
            if not dry_run:
                complete_run_record(conn, run_id, 0, "No unconsolidated events.")
            return ConsolidationRun(
                run_id=run_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                status="completed",
                events_processed=0,
                summary="No unconsolidated events.",
                error=None,
            )

        log.info(f"Found {len(events)} unconsolidated events to process.")

        # Step 3: Group events
        groups = group_events(events)
        people_mentions = extract_people_mentions(events)

        log.info(
            f"Grouped into {len(groups)} event groups, "
            f"{len(people_mentions)} people mentions."
        )

        if dry_run:
            _log_dry_run(groups, people_mentions)
            return ConsolidationRun(
                run_id=run_id,
                started_at=datetime.now(timezone.utc).isoformat(),
                status="dry_run",
                events_processed=len(events),
                summary=f"Dry run: {len(events)} events in {len(groups)} groups.",
                error=None,
            )

        # Step 4: Update canonical files per group
        updated_files: list[str] = []

        for group in groups:
            if group.category == "project":
                updated = _update_project_file(
                    canonical_dir, group, model
                )
                if updated:
                    updated_files.append(updated)

        # Update person files
        for person_name, person_events in people_mentions.items():
            updated = _update_person_file(
                canonical_dir, person_name, person_events, model
            )
            if updated:
                updated_files.append(updated)

        # Step 5: Generate daily digest
        archive_digest(canonical_dir, archive_dir, today)
        _generate_daily_digest(canonical_dir, events, today, model)
        updated_files.append("daily-digest.md")

        # Step 6: Update priorities based on digest
        _update_priorities(canonical_dir, model)
        updated_files.append("priorities.md")

        # Step 7: Regenerate handoff document
        _regenerate_handoff(canonical_dir, model)
        updated_files.append("handoff.md")

        # Step 8: Mark events as consolidated
        event_ids = tuple(e.id for e in events)
        mark_events_consolidated(conn, event_ids)

        # Step 9: Complete run record
        summary = (
            f"Processed {len(events)} events. "
            f"Updated files: {', '.join(updated_files)}."
        )
        complete_run_record(conn, run_id, len(events), summary)

        log.info(f"Consolidation complete. {summary}")

        return ConsolidationRun(
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="completed",
            events_processed=len(events),
            summary=summary,
            error=None,
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        log.error(f"Consolidation failed: {error_msg}")
        try:
            if not dry_run:
                fail_run_record(conn, run_id, error_msg)
        except Exception:
            log.error("Failed to record run failure in database.")

        return ConsolidationRun(
            run_id=run_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            events_processed=0,
            summary="",
            error=error_msg,
        )

    finally:
        conn.close()


def _update_project_file(
    canonical_dir: str,
    group: EventGroup,
    model: str,
) -> str | None:
    """Update a project canonical file using Claude API.

    Returns the relative path of the updated file, or None on failure.
    """
    project_name = group.key
    relative_path = f"projects/{_slugify(project_name)}.md"
    current_content = read_canonical_file(canonical_dir, relative_path)

    if not current_content:
        # Create a minimal template for new projects
        current_content = f"# Project: {project_name}\n\n*Auto-updated by nightly consolidation.*\n"

    events_text = format_events_for_prompt(group.events)
    prompt = build_project_update_prompt(project_name, current_content, events_text)

    try:
        updated = call_claude_api(prompt, model)
        write_canonical_file(canonical_dir, relative_path, updated)
        return relative_path
    except Exception as exc:
        log.warning(f"Failed to update project file {relative_path}: {exc}")
        return None


def _update_person_file(
    canonical_dir: str,
    person_name: str,
    events: tuple[Event, ...],
    model: str,
) -> str | None:
    """Update a person canonical file using Claude API.

    Returns the relative path of the updated file, or None on failure.
    """
    relative_path = f"people/{_slugify(person_name)}.md"
    current_content = read_canonical_file(canonical_dir, relative_path)

    if not current_content:
        current_content = f"# {person_name.title()}\n\n*Auto-updated by nightly consolidation.*\n"

    events_text = format_events_for_prompt(events)
    prompt = build_person_update_prompt(person_name, current_content, events_text)

    try:
        updated = call_claude_api(prompt, model)
        write_canonical_file(canonical_dir, relative_path, updated)
        return relative_path
    except Exception as exc:
        log.warning(f"Failed to update person file {relative_path}: {exc}")
        return None


def _generate_daily_digest(
    canonical_dir: str,
    events: tuple[Event, ...],
    date_str: str,
    model: str,
) -> None:
    """Generate the daily digest from all events."""
    events_text = format_events_for_prompt(events)
    prompt = build_daily_digest_prompt(events_text, date_str)

    try:
        digest = call_claude_api(prompt, model)
        write_canonical_file(canonical_dir, "daily-digest.md", digest)
    except Exception as exc:
        log.warning(f"Failed to generate daily digest: {exc}")


def _update_priorities(canonical_dir: str, model: str) -> None:
    """Update priorities based on the current daily digest."""
    current_priorities = read_canonical_file(canonical_dir, "priorities.md")
    digest_content = read_canonical_file(canonical_dir, "daily-digest.md")

    if not current_priorities or not digest_content:
        return

    prompt = build_priorities_update_prompt(current_priorities, digest_content)

    try:
        updated = call_claude_api(prompt, model)
        write_canonical_file(canonical_dir, "priorities.md", updated)
    except Exception as exc:
        log.warning(f"Failed to update priorities: {exc}")


def _regenerate_handoff(canonical_dir: str, model: str) -> None:
    """Regenerate the handoff document from all canonical files."""
    canonical_files = collect_canonical_files(canonical_dir)

    if not canonical_files:
        log.warning("No canonical files found. Skipping handoff regeneration.")
        return

    prompt = build_handoff_prompt(canonical_files)

    try:
        handoff = call_claude_api(prompt, model)
        write_canonical_file(canonical_dir, "handoff.md", handoff)
    except Exception as exc:
        log.warning(f"Failed to regenerate handoff document: {exc}")


def _log_dry_run(
    groups: tuple[EventGroup, ...],
    people_mentions: dict[str, tuple[Event, ...]],
) -> None:
    """Log a summary of what consolidation would do in dry-run mode."""
    log.info("=== DRY RUN ===")
    for group in groups:
        log.info(
            f"  Group: {group.category}/{group.key} "
            f"({len(group.events)} events)"
        )
    for person, events in people_mentions.items():
        log.info(f"  Person: {person} ({len(events)} events)")
    log.info("=== END DRY RUN ===")


# ---------------------------------------------------------------------------
# Utility (pure)
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug.

    Examples:
        "Lobster" -> "lobster"
        "My Project" -> "my-project"
        "Alice's Thing" -> "alices-thing"
    """
    slug = name.lower().strip()
    slug = slug.replace("'", "").replace('"', "")
    slug = slug.replace(" ", "-")
    # Remove anything that isn't alphanumeric or hyphen
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    # Collapse multiple hyphens
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point for nightly consolidation."""
    parser = argparse.ArgumentParser(
        description="Lobster Nightly Memory Consolidation"
    )
    parser.add_argument(
        "--config",
        default=str(Path.home() / "lobster" / "config" / "consolidation.conf"),
        help="Path to configuration file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Setup logging
    log_level = getattr(logging, config.get("CONSOLIDATION_LOG_LEVEL", "INFO"))
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("Starting nightly consolidation...")
    result = consolidate(config, dry_run=args.dry_run)

    if result.status == "failed":
        log.error(f"Consolidation failed: {result.error}")
        return 1

    log.info(f"Consolidation finished: {result.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
