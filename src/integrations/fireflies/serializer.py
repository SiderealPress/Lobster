"""
Fireflies transcript serializer.

Pure functions that convert FirefliesTranscript dataclass objects into clean
Markdown files with YAML frontmatter — mirrors integrations.granola.serializer
but leads with an "Action Items" section, since the driving use case (per the
Eloso team's request) is turning sales call transcripts into a CRM with next
actions. Burying action items inside a generic summary blob would defeat that
purpose, so they get their own labelled, easy-to-find section.

No I/O, no network calls — independently testable.

Output format per transcript:
    ---
    id: abc123
    title: "Sales call with Acme"
    date: 2026-06-01
    ...
    source: fireflies
    fireflies_account: primary
    ---

    # Sales call with Acme

    ## Action Items

    - Follow up with Acme on pricing
    - Send proposal by Friday

    ## Overview

    <overview text>

    ## Transcript

    **Alex** (00:00 → 00:02)
    Thanks for joining today.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from integrations.fireflies.client import FirefliesSentence, FirefliesTranscript


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------


def _slugify(text: str, max_len: int = 60) -> str:
    """
    Convert a title to a URL-safe slug for use in filenames.

    Examples:
        "Sales call with Acme" → "sales-call-with-acme"
        "Q3 Renewal: Acme Corp" → "q3-renewal-acme-corp"
    """
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        truncated = s[:max_len].rsplit("-", 1)[0]
        s = truncated if truncated else s[:max_len]
    return s or "untitled"


def transcript_filename(transcript: FirefliesTranscript) -> str:
    """
    Generate the filename (without directory path) for a transcript.

    Format: ``YYYY-MM-DD-{slug}.md``, using the transcript's `date` field.
    Falls back to today's date if `date` is missing.
    """
    dt = transcript.date or datetime.now(timezone.utc)
    date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(transcript.title)
    return f"{date_str}-{slug}.md"


def transcript_vault_path(transcript: FirefliesTranscript) -> str:
    """
    Return the relative vault path (from vault root) for a transcript.

    Format: ``fireflies/YYYY/MM/{filename}`` — mirrors granola/YYYY/MM/....
    """
    dt = transcript.date or datetime.now(timezone.utc)
    year = dt.astimezone(timezone.utc).strftime("%Y")
    month = dt.astimezone(timezone.utc).strftime("%m")
    filename = transcript_filename(transcript)
    return f"fireflies/{year}/{month}/{filename}"


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


def _format_hms(seconds: Optional[float]) -> str:
    """Format a float seconds offset as MM:SS for transcript speaker labels."""
    if seconds is None:
        return "00:00"
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# Transcript body formatting
# ---------------------------------------------------------------------------


def _format_sentences(sentences: list[FirefliesSentence]) -> str:
    """Format transcript sentences into readable Markdown, grouped by speaker."""
    if not sentences:
        return "_No transcript available._\n"

    lines: list[str] = []
    current_speaker: Optional[str] = None

    for sent in sentences:
        speaker = sent.speaker_name or "Unknown"
        if speaker != current_speaker:
            if lines:
                lines.append("")
            lines.append(f"**{speaker}** ({_format_hms(sent.start_time)} → {_format_hms(sent.end_time)})")
            current_speaker = speaker
        text = sent.text.strip()
        if text:
            lines.append(text)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main serializer
# ---------------------------------------------------------------------------


def transcript_to_markdown(transcript: FirefliesTranscript) -> str:
    """
    Convert a FirefliesTranscript → clean Markdown string with YAML frontmatter.

    Args:
        transcript: A fully populated FirefliesTranscript (from get_transcript()).

    Returns:
        A UTF-8 Markdown string ready to write to disk.
    """
    # --- Attendees YAML block ---
    attendees_yaml_lines: list[str] = []
    for attendee in transcript.meeting_attendees:
        name_q = _yaml_str(attendee.name) if attendee.name else '""'
        email_q = _yaml_str(attendee.email) if attendee.email else '""'
        attendees_yaml_lines.append(f"  - name: {name_q}")
        attendees_yaml_lines.append(f"    email: {email_q}")
    attendees_yaml = "\n".join(attendees_yaml_lines) if attendees_yaml_lines else "  []"

    duration_str = str(int(transcript.duration)) if transcript.duration is not None else "null"

    date_str = (
        transcript.date.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if transcript.date
        else ""
    )

    frontmatter = f"""---
id: {transcript.id}
title: {_yaml_str(transcript.title)}
date: {date_str}
date_iso: {_format_dt(transcript.date)}
duration_seconds: {duration_str}
host_email: {_yaml_str(transcript.host_email)}
organizer_email: {_yaml_str(transcript.organizer_email)}
transcript_url: {_yaml_str(transcript.transcript_url)}
meeting_link: {_yaml_str(transcript.meeting_link)}
attendees:
{attendees_yaml}
keywords: {transcript.summary.keywords!r}
source: fireflies
fireflies_account: {transcript.fireflies_account}
---"""

    title_line = f"# {transcript.title}"

    # Action items — leads the body (see module docstring for rationale).
    action_items = transcript.summary.action_items.strip()
    if action_items:
        action_items_section = f"## Action Items\n\n{action_items}"
    else:
        action_items_section = "## Action Items\n\n_No action items were generated for this call._"

    # Overview
    if transcript.summary.overview:
        overview_section = f"## Overview\n\n{transcript.summary.overview.strip()}"
    else:
        overview_section = "## Overview\n\n_No overview available._"

    # Transcript
    transcript_body = _format_sentences(transcript.sentences)
    transcript_section = f"## Transcript\n\n{transcript_body}"

    body_parts = [title_line, "", action_items_section, "", overview_section, "", transcript_section]
    body = "\n".join(body_parts)

    return frontmatter + "\n\n" + body + "\n"
