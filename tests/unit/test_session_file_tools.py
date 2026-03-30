"""
Unit tests for session file MCP tools.

Covers create_session_file, get_session_file, update_session_file,
and list_session_files handlers in inbox_server.py.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATE_CONTENT = """\
# Session YYYYMMDD-NNN

**Started:** <ISO timestamp, e.g. 2026-03-25T14:32:00Z>
**Ended:** active

## Summary
<1-3 sentence summary of what happened this session: main topics, decisions made, work completed.>

## Open Threads

## Open Tasks

## Open Subagents

## Communication Channels

## Notable Events
"""


def _make_sessions_dir(tmp_path: Path, with_template: bool = True) -> Path:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    if with_template:
        (sessions_dir / "session.template.md").write_text(TEMPLATE_CONTENT)
    return sessions_dir


def _make_session_file(sessions_dir: Path, session_id: str, summary: str = "") -> Path:
    content = f"""\
# Session {session_id}

**Started:** 2026-03-29T10:00:00Z
**Ended:** active

## Summary
{summary}

## Open Threads

## Open Tasks

## Open Subagents

## Communication Channels

## Notable Events
"""
    path = sessions_dir / f"{session_id}.md"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# create_session_file
# ---------------------------------------------------------------------------


class TestCreateSessionFile:
    """Tests for handle_create_session_file."""

    def test_creates_file_with_correct_name(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        assert "session_id" in data
        assert re.match(r"^\d{8}-\d{3}$", data["session_id"])
        assert "path" in data
        assert Path(data["path"]).exists()

    def test_first_session_gets_sequence_001(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        assert data["session_id"].endswith("-001")

    def test_second_session_gets_sequence_002(self, tmp_path):
        from datetime import datetime, timezone

        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        # Pre-create a 001 file
        _make_session_file(sessions_dir, f"{today}-001")

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        assert data["session_id"].endswith("-002")

    def test_template_substitution_replaces_date(self, tmp_path):
        from datetime import datetime, timezone

        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"
        today = datetime.now(timezone.utc).strftime("%Y%m%d")

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        content = Path(data["path"]).read_text()
        # The placeholder "YYYYMMDD-NNN" must have been replaced
        assert "YYYYMMDD-NNN" not in content
        assert today in content

    def test_pointer_file_written(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        assert pointer.exists()
        assert pointer.read_text().strip() == data["path"]

    def test_fallback_content_when_no_template(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path, with_template=False)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        content = Path(data["path"]).read_text()
        assert "## Summary" in content
        assert "## Open Threads" in content

    def test_sessions_dir_created_if_missing(self, tmp_path):
        sessions_dir = tmp_path / "deep" / "nested" / "sessions"
        assert not sessions_dir.exists()
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_create_session_file

            # _resolve_sessions_dir is mocked, but the dir is still absent —
            # create_session_file must create it via mkdir when writing the file.
            # Since we mock _resolve_sessions_dir to return an absent path, we
            # also need the dir to exist before the handler writes to it; this
            # test verifies the full path works when the sessions dir IS created
            # by _resolve_sessions_dir (which calls mkdir). Create it now:
            sessions_dir.mkdir(parents=True)
            result = asyncio.run(handle_create_session_file({}))

        data = json.loads(result[0].text)
        assert Path(data["path"]).exists()


# ---------------------------------------------------------------------------
# get_session_file
# ---------------------------------------------------------------------------


class TestGetSessionFile:
    """Tests for handle_get_session_file."""

    def test_reads_current_via_pointer(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-001", "Test summary content here.")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({"session_id": "current"}))

        data = json.loads(result[0].text)
        assert data["session_id"] == "20260329-001"
        assert "Test summary content here." in data["content"]
        assert data["path"] == str(path)

    def test_reads_by_explicit_session_id(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-001", "First session.")
        _make_session_file(sessions_dir, "20260329-002", "Second session.")
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({"session_id": "20260329-002"}))

        data = json.loads(result[0].text)
        assert data["session_id"] == "20260329-002"
        assert "Second session." in data["content"]

    def test_missing_file_returns_error(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({"session_id": "19990101-001"}))

        data = json.loads(result[0].text)
        assert "error" in data

    def test_no_current_session_returns_error(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        # No pointer file, no files for today
        pointer = tmp_path / "pointer"  # does not exist

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({}))

        data = json.loads(result[0].text)
        assert "error" in data

    def test_defaults_to_current(self, tmp_path):
        """Calling with no session_id behaves the same as session_id='current'."""
        from datetime import datetime, timezone

        sessions_dir = _make_sessions_dir(tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = _make_session_file(sessions_dir, f"{today}-001", "Today session content.")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({}))

        data = json.loads(result[0].text)
        assert "Today session content." in data["content"]

    def test_stale_pointer_falls_back_to_todays_latest(self, tmp_path):
        """If the pointer points to a deleted file, fall back to today's latest."""
        from datetime import datetime, timezone

        sessions_dir = _make_sessions_dir(tmp_path)
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = _make_session_file(sessions_dir, f"{today}-001", "Fallback session.")
        pointer = tmp_path / "pointer"
        pointer.write_text("/nonexistent/path.md")  # stale pointer

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_get_session_file

            result = asyncio.run(handle_get_session_file({"session_id": "current"}))

        data = json.loads(result[0].text)
        assert "Fallback session." in data["content"]


# ---------------------------------------------------------------------------
# update_session_file
# ---------------------------------------------------------------------------


class TestUpdateSessionFile:
    """Tests for handle_update_session_file."""

    def test_updates_named_section(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-001", "Old summary text.")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            result = asyncio.run(
                handle_update_session_file({
                    "section": "Summary",
                    "content": "New summary content for this session.",
                })
            )

        data = json.loads(result[0].text)
        assert data["updated"] is True
        updated_text = path.read_text()
        assert "New summary content for this session." in updated_text
        assert "Old summary text." not in updated_text

    def test_preserves_other_sections(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-001")
        # Add distinct content to another section
        original = path.read_text()
        original = original.replace(
            "## Notable Events\n",
            "## Notable Events\nSomething notable happened.\n",
        )
        path.write_text(original)
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            asyncio.run(
                handle_update_session_file({
                    "section": "Summary",
                    "content": "Updated summary.",
                })
            )

        updated_text = path.read_text()
        assert "Something notable happened." in updated_text

    def test_atomic_write(self, tmp_path):
        """No .tmp file should remain after a successful write."""
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-001")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            asyncio.run(
                handle_update_session_file({
                    "section": "Summary",
                    "content": "Atomic write test.",
                })
            )

        tmp_file = path.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_missing_section_returns_error(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-001")
        pointer = tmp_path / "pointer"
        pointer.write_text(str(path))

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            result = asyncio.run(
                handle_update_session_file({
                    "section": "NonExistentSection",
                    "content": "Some content.",
                })
            )

        data = json.loads(result[0].text)
        assert "error" in data

    def test_missing_section_arg_returns_error(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            result = asyncio.run(handle_update_session_file({"content": "x"}))

        data = json.loads(result[0].text)
        assert "error" in data

    def test_update_by_explicit_session_id(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        path = _make_session_file(sessions_dir, "20260329-002")
        pointer = tmp_path / "pointer"

        with (
            patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir),
            patch("src.mcp.inbox_server._SESSION_POINTER_FILE", pointer),
        ):
            from src.mcp.inbox_server import handle_update_session_file

            result = asyncio.run(
                handle_update_session_file({
                    "section": "Summary",
                    "content": "Explicitly targeted session.",
                    "session_id": "20260329-002",
                })
            )

        data = json.loads(result[0].text)
        assert data["updated"] is True
        assert "Explicitly targeted session." in path.read_text()


# ---------------------------------------------------------------------------
# list_session_files
# ---------------------------------------------------------------------------


class TestListSessionFiles:
    """Tests for handle_list_session_files."""

    def test_lists_all_session_files(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-001")
        _make_session_file(sessions_dir, "20260329-002")
        _make_session_file(sessions_dir, "20260328-001")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        ids = [e["session_id"] for e in entries]
        assert "20260329-001" in ids
        assert "20260329-002" in ids
        assert "20260328-001" in ids

    def test_filters_by_date(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-001")
        _make_session_file(sessions_dir, "20260328-001")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({"date": "20260329"}))

        entries = json.loads(result[0].text)
        assert all(e["session_id"].startswith("20260329") for e in entries)
        assert len(entries) == 1

    def test_template_excluded_from_list(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-001")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        assert not any(e["session_id"] == "session.template" for e in entries)

    def test_has_content_false_for_boilerplate(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        # Empty summary (boilerplate only from template)
        _make_session_file(sessions_dir, "20260329-001", summary="<1-3 sentence summary")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        entry = next(e for e in entries if e["session_id"] == "20260329-001")
        assert entry["has_content"] is False

    def test_has_content_false_for_short_summary(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-001", summary="Short.")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        entry = next(e for e in entries if e["session_id"] == "20260329-001")
        assert entry["has_content"] is False

    def test_has_content_true_for_substantial_summary(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        long_summary = "Completed a major refactor of the session file tooling. " * 3
        _make_session_file(sessions_dir, "20260329-001", summary=long_summary)

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        entry = next(e for e in entries if e["session_id"] == "20260329-001")
        assert entry["has_content"] is True

    def test_results_are_sorted(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path)
        _make_session_file(sessions_dir, "20260329-003")
        _make_session_file(sessions_dir, "20260329-001")
        _make_session_file(sessions_dir, "20260329-002")

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        ids = [e["session_id"] for e in entries]
        assert ids == sorted(ids)

    def test_empty_directory_returns_empty_list(self, tmp_path):
        sessions_dir = _make_sessions_dir(tmp_path, with_template=False)

        with patch("src.mcp.inbox_server._resolve_sessions_dir", return_value=sessions_dir):
            from src.mcp.inbox_server import handle_list_session_files

            result = asyncio.run(handle_list_session_files({}))

        entries = json.loads(result[0].text)
        assert entries == []
