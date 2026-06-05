"""
Granola note serializer — Slice 1.

Pure functions that convert GranolaNote dataclass objects into clean
Markdown files with YAML frontmatter.

No I/O, no network calls — independently testable.

Output format per note:
    ---
    id: not_xeEBpfpKDHxtv6
    title: "Ben Roome and Sarah Gowe"
    date: 2026-03-27
    created_at: "2026-03-27T21:30:24Z"
    updated_at: "2026-03-27T21:59:08Z"
    owner_name: "Alex Example"
    owner_email: "alex@example.com"
    attendees:
      - name: "Alice Smith"
        email: "alice@example.com"
    duration_minutes: 28
    calendar_title: "Ben Roome and Sarah Gowe"
    ---

    # Ben Roome and Sarah Gowe

    ## Summary

    <summary_markdown content>

    ## Transcript

    **Speaker Name** (00:00 → 01:23)
    transcript text here...
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from integrations.granola.client import GranolaNote, GranolaTranscriptSegment


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------


def _slugify(text: str, max_len: int = 60) -> str:
    """
    Convert a title to a URL-safe slug for use in filenames.

    Examples:
        "Ben Roome and Sarah Gowe" → "ben-roome-and-sarah-gowe"
        "Q3 Planning: Finance & Ops" → "q3-planning-finance-ops"
    """
    # Lowercase
    s = text.lower()
    # Replace non-alphanumeric (except spaces) with nothing
    s = re.sub(r"[^\w\s-]", "", s)
    # Replace whitespace / underscores with dashes
    s = re.sub(r"[\s_]+", "-", s)
    # Remove leading/trailing dashes
    s = s.strip("-")
    # Truncate
    if len(s) > max_len:
        # Try to truncate at a word boundary
        truncated = s[:max_len].rsplit("-", 1)[0]
        s = truncated if truncated else s[:max_len]
    return s or "untitled"


def note_filename(note: GranolaNote) -> str:
    """
    Generate the filename (without directory path) for a note.

    Format: ``YYYY-MM-DD-{slug}.md``
    Uses the calendar event scheduled start time if available,
    otherwise falls back to created_at.
    """
    dt = note.created_at
    if note.calendar_event and note.calendar_event.scheduled_start_time:
        dt = note.calendar_event.scheduled_start_time

    date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(note.title)
    return f"{date_str}-{slug}.md"


def note_vault_path(note: GranolaNote) -> str:
    """
    Return the relative vault path (from vault root) for a note.

    Format: ``granola/YYYY/MM/{filename}``
    """
    dt = note.created_at
    if note.calendar_event and note.calendar_event.scheduled_start_time:
        dt = note.calendar_event.scheduled_start_time

    year = dt.astimezone(timezone.utc).strftime("%Y")
    month = dt.astimezone(timezone.utc).strftime("%m")
    filename = note_filename(note)
    return f"granola/{year}/{month}/{filename}"


# ---------------------------------------------------------------------------
# Duration calculation
# ---------------------------------------------------------------------------


def _duration_minutes(note: GranolaNote) -> Optional[int]:
    """Return meeting duration in minutes, or None if not determinable."""
    if note.calendar_event:
        start = note.calendar_event.scheduled_start_time
        end = note.calendar_event.scheduled_end_time
        if start and end:
            delta = end - start
            return max(0, int(delta.total_seconds() / 60))

    # Fallback: use transcript start/end if available
    if note.transcript:
        # Transcript items have string timestamps — not always parseable
        pass

    return None


# ---------------------------------------------------------------------------
# YAML frontmatter helpers
# ---------------------------------------------------------------------------


def _yaml_str(value: str) -> str:
    """Wrap a string in double quotes, escaping any existing double quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_dt(dt: Optional[datetime]) -> str:
    """Format a datetime for YAML frontmatter."""
    if dt is None:
        return '""'
    return _yaml_str(dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------


def _format_transcript_segments(segments: list[GranolaTranscriptSegment]) -> str:
    """Format transcript segments into readable Markdown."""
    if not segments:
        return "_No transcript available._\n"

    lines: list[str] = []
    current_speaker: Optional[str] = None

    for seg in segments:
        speaker = seg.speaker or "Unknown"
        if speaker != current_speaker:
            # New speaker block
            if lines:
                lines.append("")
            lines.append(f"**{speaker}**")
            current_speaker = speaker
        # Append text (strip to clean up whitespace)
        text = seg.text.strip()
        if text:
            lines.append(text)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main serializer
# ---------------------------------------------------------------------------


def note_to_markdown(note: GranolaNote) -> str:
    """
    Convert a GranolaNote → clean Markdown string with YAML frontmatter.

    This is the primary output format for the Obsidian vault. The result
    is a self-contained .md file with all metadata in frontmatter and
    content in the body.

    Args:
        note: A fully populated GranolaNote (from get_note with transcript).

    Returns:
        A UTF-8 Markdown string ready to write to disk.
    """
    # --- Build attendees YAML block ---
    attendees_yaml_lines: list[str] = []
    for attendee in note.attendees:
        name_q = _yaml_str(attendee.name) if attendee.name else '""'
        email_q = _yaml_str(attendee.email) if attendee.email else '""'
        attendees_yaml_lines.append(f'  - name: {name_q}')
        attendees_yaml_lines.append(f'    email: {email_q}')
    attendees_yaml = "\n".join(attendees_yaml_lines) if attendees_yaml_lines else "  []"

    # --- Duration ---
    duration_min = _duration_minutes(note)
    duration_str = str(duration_min) if duration_min is not None else "null"

    # --- Calendar event fields ---
    cal = note.calendar_event
    calendar_title_str = _yaml_str(cal.event_title) if cal and cal.event_title else '""'
    scheduled_start_str = _format_dt(cal.scheduled_start_time) if cal else '""'
    scheduled_end_str = _format_dt(cal.scheduled_end_time) if cal else '""'

    # --- Frontmatter ---
    frontmatter = f"""---
id: {note.id}
title: {_yaml_str(note.title)}
date: {note.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")}
created_at: {_format_dt(note.created_at)}
updated_at: {_format_dt(note.updated_at)}
owner_name: {_yaml_str(note.owner.name)}
owner_email: {_yaml_str(note.owner.email)}
attendees:
{attendees_yaml}
duration_minutes: {duration_str}
calendar_title: {calendar_title_str}
scheduled_start: {scheduled_start_str}
scheduled_end: {scheduled_end_str}
source: granola
granola_account: {note.granola_account}
---"""

    # --- Body ---
    title_line = f"# {note.title}"

    # Summary section
    if note.summary_markdown:
        summary_section = f"## Summary\n\n{note.summary_markdown.strip()}"
    elif note.summary_text:
        summary_section = f"## Summary\n\n{note.summary_text.strip()}"
    else:
        summary_section = "## Summary\n\n_No summary available._"

    # Transcript section
    if note.transcript:
        transcript_body = _format_transcript_segments(note.transcript)
        transcript_section = f"## Transcript\n\n{transcript_body}"
    else:
        transcript_section = ""

    # Assemble body
    body_parts = [title_line, "", summary_section]
    if transcript_section:
        body_parts.extend(["", transcript_section])

    body = "\n".join(body_parts)

    return frontmatter + "\n\n" + body + "\n"
