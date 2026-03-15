"""
Tests for handle_write_observation MCP tool handler.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/mcp is on sys.path so that `reliability` (a sibling module) can
# be resolved when inbox_server is imported via the `src.mcp.inbox_server`
# dotted path.  The root conftest adds `src/` but not `src/mcp/`, so we add
# the latter here; this is a no-op if the path is already present.
_MCP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Pre-load the module so that unittest.mock can resolve "src.mcp.inbox_server"
# as an attribute of the `src.mcp` package before patch.multiple opens.
import src.mcp.inbox_server  # noqa: F401


class TestHandleWriteObservation:
    """Tests for the write_observation handler."""

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        """Get inbox directory."""
        return temp_messages_dir / "inbox"

    def _run(self, args: dict, inbox_dir: Path) -> list:
        # Import inside the patch block, matching the pattern used throughout
        # the existing MCP server test suite.
        # Always mock _DEBUG_MODE=False/_DEBUG_RESOLVED=True so tests that
        # expect inbox writes are not affected by the host LOBSTER_DEBUG setting.
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=False,
            _DEBUG_RESOLVED=True,
        ):
            from src.mcp.inbox_server import handle_write_observation
            return asyncio.run(handle_write_observation(args))

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_valid_observation_writes_file(self, inbox_dir: Path):
        """Valid args produce a file in the inbox with correct fields."""
        result = self._run(
            {
                "chat_id": 8305714125,
                "text": "User prefers metric units.",
                "category": "user_context",
            },
            inbox_dir,
        )

        assert len(result) == 1
        assert "Observation queued" in result[0].text

        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1

        content = json.loads(files[0].read_text())
        assert content["type"] == "subagent_observation"
        assert content["category"] == "user_context"
        assert content["chat_id"] == 8305714125
        assert content["text"] == "User prefers metric units."
        assert content["source"] == "telegram"  # default

    def test_system_context_category_accepted(self, inbox_dir: Path):
        """system_context is a valid category."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "Config drift detected.",
                "category": "system_context",
            },
            inbox_dir,
        )
        assert "Observation queued" in result[0].text
        files = list(inbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content["category"] == "system_context"

    def test_system_error_category_accepted(self, inbox_dir: Path):
        """system_error is a valid category."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "Side call to memory API failed.",
                "category": "system_error",
            },
            inbox_dir,
        )
        assert "Observation queued" in result[0].text

    def test_optional_task_id_included_when_provided(self, inbox_dir: Path):
        """task_id is written to the message when supplied."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "Observation with task reference.",
                "category": "system_context",
                "task_id": "my-task-42",
            },
            inbox_dir,
        )
        assert "Observation queued" in result[0].text
        files = list(inbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content.get("task_id") == "my-task-42"

    def test_optional_task_id_omitted_when_absent(self, inbox_dir: Path):
        """task_id key is absent from the message when not supplied."""
        self._run(
            {
                "chat_id": 123,
                "text": "No task ID here.",
                "category": "system_context",
            },
            inbox_dir,
        )
        files = list(inbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert "task_id" not in content

    def test_source_parameter_overrides_default(self, inbox_dir: Path):
        """Passing source='slack' writes 'slack' into the file."""
        self._run(
            {
                "chat_id": 456,
                "text": "Slack observation.",
                "category": "user_context",
                "source": "slack",
            },
            inbox_dir,
        )
        files = list(inbox_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content["source"] == "slack"

    def test_message_ids_are_unique_for_rapid_calls(self, inbox_dir: Path):
        """Rapid sequential calls produce distinct message IDs (no collision)."""
        args = {"chat_id": 1, "text": "obs", "category": "system_error"}
        for _ in range(5):
            self._run(args, inbox_dir)

        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 5
        ids = [json.loads(f.read_text())["id"] for f in files]
        assert len(set(ids)) == 5, "Duplicate message IDs detected"

    # ------------------------------------------------------------------
    # Invalid category
    # ------------------------------------------------------------------

    def test_invalid_category_returns_error(self, inbox_dir: Path):
        """An unknown category is rejected with a descriptive error."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "Some observation.",
                "category": "banana",
            },
            inbox_dir,
        )
        assert "Error" in result[0].text
        assert "category" in result[0].text.lower()
        # No file should be written
        assert list(inbox_dir.glob("*.json")) == []

    # ------------------------------------------------------------------
    # Missing required fields
    # ------------------------------------------------------------------

    def test_missing_chat_id_returns_error(self, inbox_dir: Path):
        """Omitting chat_id returns an error and writes no file."""
        result = self._run(
            {
                "text": "Missing chat_id.",
                "category": "system_error",
            },
            inbox_dir,
        )
        assert "Error" in result[0].text
        assert "chat_id" in result[0].text
        assert list(inbox_dir.glob("*.json")) == []

    def test_missing_text_returns_error(self, inbox_dir: Path):
        """Omitting text returns an error and writes no file."""
        result = self._run(
            {
                "chat_id": 123,
                "category": "system_error",
            },
            inbox_dir,
        )
        assert "Error" in result[0].text
        assert list(inbox_dir.glob("*.json")) == []

    def test_empty_text_returns_error(self, inbox_dir: Path):
        """Blank text string is treated as missing."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "   ",
                "category": "system_error",
            },
            inbox_dir,
        )
        assert "Error" in result[0].text
        assert list(inbox_dir.glob("*.json")) == []

    def test_missing_category_returns_error(self, inbox_dir: Path):
        """Omitting category returns an error and writes no file."""
        result = self._run(
            {
                "chat_id": 123,
                "text": "No category supplied.",
            },
            inbox_dir,
        )
        assert "Error" in result[0].text
        assert list(inbox_dir.glob("*.json")) == []

    # ------------------------------------------------------------------
    # observation_type parameter (Change 1)
    # ------------------------------------------------------------------

    def test_observation_type_default_is_preference(self, inbox_dir: Path):
        """When observation_type is omitted, user model dispatch uses 'preference'."""
        dispatched: list[dict] = []

        class FakeUserModel:
            def dispatch(self, tool_name: str, args: dict) -> str:
                dispatched.append({"tool": tool_name, "args": args})
                return "{}"

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=FakeUserModel(),
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User prefers morning meetings.",
                "category": "user_context",
            }))

        assert len(dispatched) == 1
        call = dispatched[0]
        assert call["tool"] == "model_observe"
        assert call["args"]["observation_type"] == "preference"
        assert call["args"]["observation"] == "User prefers morning meetings."
        assert call["args"]["confidence"] == 0.75

    def test_observation_type_explicit_value_forwarded(self, inbox_dir: Path):
        """When observation_type is supplied, it is forwarded to user model dispatch."""
        dispatched: list[dict] = []

        class FakeUserModel:
            def dispatch(self, tool_name: str, args: dict) -> str:
                dispatched.append({"tool": tool_name, "args": args})
                return "{}"

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=FakeUserModel(),
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User seems energized this morning.",
                "category": "user_context",
                "observation_type": "energy",
            }))

        assert len(dispatched) == 1
        assert dispatched[0]["args"]["observation_type"] == "energy"

    def test_user_context_without_user_model_still_writes_file(self, inbox_dir: Path):
        """When _user_model is None, user_context observation still writes to inbox."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=None,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User prefers dark mode.",
                "category": "user_context",
            }))

        assert "Observation queued" in result[0].text
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1

    def test_non_user_context_does_not_call_user_model(self, inbox_dir: Path):
        """system_context and system_error observations skip user model dispatch."""
        dispatched: list[dict] = []

        class FakeUserModel:
            def dispatch(self, tool_name: str, args: dict) -> str:
                dispatched.append({"tool": tool_name, "args": args})
                return "{}"

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=FakeUserModel(),
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Config drift detected.",
                "category": "system_context",
            }))
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "API call failed.",
                "category": "system_error",
            }))

        assert dispatched == [], "User model should not be called for non-user_context observations"

    def test_user_model_dispatch_failure_is_non_fatal(self, inbox_dir: Path):
        """If user model dispatch raises, the observation is still queued to inbox."""
        class BrokenUserModel:
            def dispatch(self, tool_name: str, args: dict) -> str:
                raise RuntimeError("DB connection lost")

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=BrokenUserModel(),
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User prefers async communication.",
                "category": "user_context",
            }))

        # Despite dispatch failure, the inbox file should still be written
        assert "Observation queued" in result[0].text
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1

    # ------------------------------------------------------------------
    # Debug mode: automatic routing (LOBSTER_DEBUG=true)
    # ------------------------------------------------------------------

    def test_system_context_bypasses_inbox_in_debug_mode(self, inbox_dir: Path):
        """system_context observations bypass the inbox when LOBSTER_DEBUG=true."""
        emitted: list[dict] = []

        def fake_emit(text: str, category: str = "system_context") -> None:
            emitted.append({"text": text, "category": category})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Config drift detected.",
                "category": "system_context",
            }))

        # No inbox file — bypassed entirely
        assert list(inbox_dir.glob("*.json")) == []
        # Direct Telegram delivery
        assert len(emitted) == 1
        assert emitted[0]["category"] == "system_context"
        assert "Config drift detected." in emitted[0]["text"]
        assert "debug mode" in result[0].text

    def test_system_error_bypasses_inbox_in_debug_mode(self, inbox_dir: Path):
        """system_error observations bypass the inbox when LOBSTER_DEBUG=true."""
        emitted: list[dict] = []

        def fake_emit(text: str, category: str = "system_context") -> None:
            emitted.append({"text": text, "category": category})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "API call failed.",
                "category": "system_error",
            }))

        assert list(inbox_dir.glob("*.json")) == []
        assert len(emitted) == 1
        assert emitted[0]["category"] == "system_error"
        assert "debug mode" in result[0].text

    def test_user_context_still_queues_inbox_in_debug_mode(self, inbox_dir: Path):
        """user_context observations always write to inbox even when LOBSTER_DEBUG=true."""
        emitted: list[dict] = []

        def fake_emit(text: str, category: str = "system_context") -> None:
            emitted.append({"text": text, "category": category})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User prefers dark mode.",
                "category": "user_context",
            }))

        # Inbox file written (dispatcher needs to act on user_context)
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        # Also emitted directly so user sees it in debug mode
        assert len(emitted) == 1
        assert emitted[0]["category"] == "user_context"
        assert "Observation queued" in result[0].text

    def test_system_context_goes_to_inbox_in_non_debug_mode(self, inbox_dir: Path):
        """system_context observations write to inbox when LOBSTER_DEBUG=false."""
        emitted: list[dict] = []

        def fake_emit(text: str, category: str = "system_context") -> None:
            emitted.append({"text": text, "category": category})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=False,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Config state normal.",
                "category": "system_context",
            }))

        # Goes to inbox — dispatcher handles it
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        # No direct Telegram delivery
        assert emitted == []
        assert "Observation queued" in result[0].text

    def test_debug_bypass_includes_task_id_in_emitted_text(self, inbox_dir: Path):
        """When bypassing inbox in debug mode, task_id is appended to the emitted text."""
        emitted: list[dict] = []

        def fake_emit(text: str, category: str = "system_context") -> None:
            emitted.append({"text": text, "category": category})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _DEBUG_MODE=True,
            _DEBUG_RESOLVED=True,
            _emit_debug_observation=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Something happened.",
                "category": "system_context",
                "task_id": "task-42",
            }))

        assert len(emitted) == 1
        assert "task-42" in emitted[0]["text"]
