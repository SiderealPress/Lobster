"""
Tests for src/integrations/fireflies/vault_writer.py

Uses a real tmp_path as the vault root (fast, no mocking needed for
filesystem ops) but mocks subprocess/git calls are exercised for real since
git is available in the test environment and operations are cheap.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from integrations.fireflies.client import ACCOUNT_PRIMARY, FirefliesTranscript  # noqa: E402
from integrations.fireflies.vault_writer import write_transcript, write_transcripts_batch  # noqa: E402


def _make_transcript(tid: str = "abc123", title: str = "Sales call") -> FirefliesTranscript:
    return FirefliesTranscript(
        id=tid,
        title=title,
        date=datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc),
    )


class TestWriteTranscript:
    def test_writes_new_file(self, tmp_path: Path):
        t = _make_transcript()
        was_written, detail = write_transcript(t, vault_path=tmp_path)
        assert was_written is True
        expected = tmp_path / "fireflies" / "2026" / "06" / "2026-06-01-sales-call.md"
        assert expected.exists()
        assert "id: abc123" in expected.read_text()

    def test_unchanged_content_is_skipped(self, tmp_path: Path):
        t = _make_transcript()
        write_transcript(t, vault_path=tmp_path)
        was_written, detail = write_transcript(t, vault_path=tmp_path)
        assert was_written is False
        assert detail == "unchanged"

    def test_changed_title_rewrites_file(self, tmp_path: Path):
        t1 = _make_transcript(title="Old Title")
        t2 = FirefliesTranscript(id="abc123", title="Old Title", date=t1.date)
        write_transcript(t1, vault_path=tmp_path)

        # Same path (title unchanged) but different content (e.g. new action items)
        t3 = FirefliesTranscript(
            id="abc123", title="Old Title", date=t1.date,
            host_email="new@example.com",
        )
        was_written, _ = write_transcript(t3, vault_path=tmp_path)
        assert was_written is True


class TestWriteTranscriptsBatch:
    def test_writes_all_and_commits(self, tmp_path: Path):
        transcripts = [_make_transcript("t1", "Call One"), _make_transcript("t2", "Call Two")]
        result = write_transcripts_batch(transcripts, vault_path=tmp_path, commit=True)
        assert result.n_written == 2
        assert result.n_skipped == 0
        assert result.n_errors == 0
        assert result.committed is True

    def test_second_run_skips_unchanged(self, tmp_path: Path):
        transcripts = [_make_transcript("t1", "Call One")]
        write_transcripts_batch(transcripts, vault_path=tmp_path, commit=True)
        result = write_transcripts_batch(transcripts, vault_path=tmp_path, commit=True)
        assert result.n_written == 0
        assert result.n_skipped == 1

    def test_commit_false_does_not_commit(self, tmp_path: Path):
        transcripts = [_make_transcript("t1", "Call One")]
        result = write_transcripts_batch(transcripts, vault_path=tmp_path, commit=False)
        assert result.committed is False

    def test_git_repo_initialised(self, tmp_path: Path):
        transcripts = [_make_transcript("t1", "Call One")]
        write_transcripts_batch(transcripts, vault_path=tmp_path, commit=True)
        assert (tmp_path / ".git").exists()
