"""
Tests for subagent_observation message display in check_inbox / wait_for_messages.

Verifies that the `category` and `task_id` fields are surfaced in the formatted
output so the dispatcher can route observations correctly.

Relates to: https://github.com/SiderealPress/lobster/issues/325
"""

import asyncio
import json
import sys
import time
from pathlib import Path
import pytest
from unittest.mock import patch

# inbox_server imports sibling modules from src/mcp/, so ensure that directory
# is on sys.path before importing.
_MCP_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "src" / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import src.mcp.inbox_server  # noqa: E402  (side-effect: registers module for patching)


def _make_observation(
    category: str = "system_context",
    text: str = "some observation text",
    task_id: str | None = "obs-task-1",
    chat_id: int = 100001,
) -> dict:
    """Build a minimal subagent_observation message dict."""
    ts_ms = int(time.time() * 1000)
    msg: dict = {
        "id": f"{ts_ms}_observation_test",
        "type": "subagent_observation",
        "source": "telegram",
        "chat_id": chat_id,
        "text": text,
        "category": category,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    if task_id is not None:
        msg["task_id"] = task_id
    return msg


class TestSubagentObservationDisplay:
    """check_inbox must surface category (and task_id) for subagent_observation."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    def _check_inbox(self, inbox_dir: Path) -> str:
        with patch.multiple("src.mcp.inbox_server", INBOX_DIR=inbox_dir):
            from src.mcp.inbox_server import handle_check_inbox
            result = asyncio.run(handle_check_inbox({}))
            return result[0].text

    # ------------------------------------------------------------------
    # category field is surfaced
    # ------------------------------------------------------------------

    def test_system_context_category_shown(self, inbox_dir: Path):
        """system_context category must appear in output."""
        msg = _make_observation(category="system_context")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "system_context" in text

    def test_user_context_category_shown(self, inbox_dir: Path):
        """user_context category must appear in output."""
        msg = _make_observation(category="user_context")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "user_context" in text

    def test_system_error_category_shown(self, inbox_dir: Path):
        """system_error category must appear in output."""
        msg = _make_observation(category="system_error")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "system_error" in text

    # ------------------------------------------------------------------
    # task_id field is surfaced when present
    # ------------------------------------------------------------------

    def test_task_id_shown_when_present(self, inbox_dir: Path):
        """task_id must appear in output when set on the message."""
        msg = _make_observation(task_id="my-task-42")
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "my-task-42" in text

    def test_task_id_absent_no_crash(self, inbox_dir: Path):
        """Messages without task_id must still render without error."""
        msg = _make_observation(task_id=None)
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        # Category still shown; no crash
        assert "system_context" in text

    # ------------------------------------------------------------------
    # Header identifies message as an observation (not a user message)
    # ------------------------------------------------------------------

    def test_observation_header_present(self, inbox_dir: Path):
        """Output must contain an OBSERVATION label so the dispatcher recognises the type."""
        msg = _make_observation()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        assert "OBSERVATION" in text

    def test_observation_does_not_show_generic_user_header(self, inbox_dir: Path):
        """subagent_observation must NOT render a 'from **Unknown**' user header."""
        msg = _make_observation()
        (inbox_dir / f"{msg['id']}.json").write_text(json.dumps(msg))

        text = self._check_inbox(inbox_dir)

        # The generic "from **Unknown**" header must not appear for this type
        assert "from **Unknown**" not in text
