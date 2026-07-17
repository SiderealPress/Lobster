"""
Tests for src/integrations/fireflies/serializer.py — pure Markdown/frontmatter
generation, no I/O. Written before the implementation (TDD).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from integrations.fireflies.client import (  # noqa: E402
    ACCOUNT_PRIMARY,
    FirefliesAttendee,
    FirefliesSentence,
    FirefliesSummary,
    FirefliesTranscript,
)
from integrations.fireflies.serializer import (  # noqa: E402
    transcript_filename,
    transcript_to_markdown,
    transcript_vault_path,
)


def _make_transcript(
    tid: str = "abc123",
    title: str = "Sales call with Acme",
    date: datetime = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc),
    account: str = ACCOUNT_PRIMARY,
    action_items: str = "- Follow up with Acme on pricing\n- Send proposal by Friday",
) -> FirefliesTranscript:
    return FirefliesTranscript(
        id=tid,
        title=title,
        date=date,
        duration=1800,
        transcript_url="https://app.fireflies.ai/view/abc123",
        meeting_link="https://meet.google.com/xyz",
        host_email="alex@example.com",
        organizer_email="alex@example.com",
        participants=["alex@example.com", "prospect@acme.com"],
        meeting_attendees=[
            FirefliesAttendee(name="Alex", email="alex@example.com"),
            FirefliesAttendee(name="Prospect Person", email="prospect@acme.com"),
        ],
        summary=FirefliesSummary(
            overview="Discovery call about Acme's CRM needs.",
            action_items=action_items,
            keywords=["CRM", "pricing"],
            outline="1. Intro\n2. Needs\n3. Next steps",
        ),
        sentences=[
            FirefliesSentence(
                index=0, speaker_name="Alex", speaker_id="1",
                text="Thanks for joining today.", start_time=0.0, end_time=2.5,
            ),
            FirefliesSentence(
                index=1, speaker_name="Prospect Person", speaker_id="2",
                text="Happy to be here.", start_time=2.5, end_time=4.0,
            ),
        ],
        fireflies_account=account,
    )


# ---------------------------------------------------------------------------
# Filenames / vault paths
# ---------------------------------------------------------------------------


class TestTranscriptFilename:
    def test_filename_format(self):
        t = _make_transcript()
        assert transcript_filename(t) == "2026-06-01-sales-call-with-acme.md"

    def test_untitled_falls_back(self):
        t = _make_transcript(title="")
        # FirefliesTranscript defaults title to "" here directly (not via parser),
        # serializer must still produce a safe filename.
        assert transcript_filename(t).endswith(".md")
        assert " " not in transcript_filename(t)


class TestTranscriptVaultPath:
    def test_path_uses_fireflies_root_and_year_month(self):
        t = _make_transcript()
        assert transcript_vault_path(t) == "fireflies/2026/06/2026-06-01-sales-call-with-acme.md"

    def test_path_falls_back_to_untitled_date_when_date_missing(self):
        t = _make_transcript(date=None)
        path = transcript_vault_path(t)
        assert path.startswith("fireflies/")
        assert path.endswith(".md")


# ---------------------------------------------------------------------------
# Markdown body / frontmatter
# ---------------------------------------------------------------------------


class TestTranscriptToMarkdown:
    def test_frontmatter_contains_core_fields(self):
        t = _make_transcript()
        md = t and transcript_to_markdown(t)
        assert "id: abc123" in md
        assert 'title: "Sales call with Acme"' in md
        assert "source: fireflies" in md
        assert f"fireflies_account: {ACCOUNT_PRIMARY}" in md

    def test_named_account_in_frontmatter(self):
        t = _make_transcript(account="jake")
        md = transcript_to_markdown(t)
        assert "fireflies_account: jake" in md

    def test_title_heading_in_body(self):
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert "# Sales call with Acme" in md

    def test_action_items_section_present(self):
        """
        The 'summarize sales calls and turn them into a CRM with next actions'
        ask means action_items must be a clearly labelled, easy-to-find section
        — not buried inside a generic summary blob.
        """
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert "## Action Items" in md
        assert "Follow up with Acme on pricing" in md
        assert "Send proposal by Friday" in md

    def test_no_action_items_shows_placeholder(self):
        t = _make_transcript(action_items="")
        md = transcript_to_markdown(t)
        assert "## Action Items" in md
        assert "_No action items" in md

    def test_overview_section_present(self):
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert "## Overview" in md
        assert "Discovery call about Acme's CRM needs." in md

    def test_transcript_section_present(self):
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert "## Transcript" in md
        assert "**Alex**" in md
        assert "Thanks for joining today." in md
        assert "**Prospect Person**" in md

    def test_no_sentences_shows_placeholder(self):
        t = _make_transcript()
        t_no_sentences = FirefliesTranscript(
            id=t.id, title=t.title, date=t.date, sentences=[],
        )
        md = transcript_to_markdown(t_no_sentences)
        assert "_No transcript available._" in md

    def test_attendees_in_frontmatter(self):
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert '"Alex"' in md
        assert '"alex@example.com"' in md

    def test_transcript_url_in_frontmatter(self):
        t = _make_transcript()
        md = transcript_to_markdown(t)
        assert "https://app.fireflies.ai/view/abc123" in md
