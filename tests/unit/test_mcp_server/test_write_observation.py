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

# Placeholder string used as a test chat_id value. Used as a bare name in
# some test assertions, so it must be defined as a module-level constant.
OWNER_CHAT_ID_PLACEHOLDER = "OWNER_CHAT_ID_PLACEHOLDER"


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
                "chat_id": OWNER_CHAT_ID_PLACEHOLDER,
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
        assert content["chat_id"] == OWNER_CHAT_ID_PLACEHOLDER
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

    def test_user_context_observation_writes_to_inbox(self, inbox_dir: Path):
        """user_context observations are written to the inbox regardless of user model state."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=None,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User prefers morning meetings.",
                "category": "user_context",
            }))

        assert "Observation queued" in result[0].text
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        msg = json.loads(files[0].read_text())
        assert msg["category"] == "user_context"
        assert msg["text"] == "User prefers morning meetings."

    def test_user_context_observation_chat_id_preserved(self, inbox_dir: Path):
        """The chat_id passed to write_observation is preserved in the inbox message."""
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _user_model=None,
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "User seems energized this morning.",
                "category": "user_context",
            }))

        files = list(inbox_dir.glob("*.json"))
        msg = json.loads(files[0].read_text())
        assert msg["chat_id"] == 123

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

    def test_system_context_writes_inbox_but_not_mirrored_in_debug_mode(self, inbox_dir: Path):
        """system_context observations write to inbox but are NOT forwarded to Telegram even when LOBSTER_DEBUG=true.

        system_context is an internal routing decision — noisy and not actionable.
        Only system_error and user_context warrant real-time debug visibility.
        """
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            category: str = "system_context",
            visibility: str = "mcp-only",
            emitter: str | None = None,
        ) -> None:
            emitted.append({"text": text, "category": category, "visibility": visibility, "emitter": emitter})

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

        # Inbox file written — dispatcher still sees it (additive, not bypass)
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        # No direct Telegram delivery — system_context is muted even in debug mode
        assert emitted == []
        assert "Observation queued" in result[0].text

    def test_system_error_writes_inbox_and_mirrors_in_debug_mode(self, inbox_dir: Path):
        """system_error observations write to inbox AND emit an event via _emit_event."""
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append({"text": text, "event_type": event_type, "severity": severity, "emitter": emitter})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _emit_event=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            result = asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "API call failed.",
                "category": "system_error",
            }))

        # Inbox file written — dispatcher still sees it (additive, not bypass)
        files = list(inbox_dir.glob("*.json"))
        assert len(files) == 1
        # Debug event also emitted via event bus
        assert len(emitted) == 1
        assert emitted[0]["event_type"] == "agent.observation.system_error"
        assert emitted[0]["severity"] == "error"
        assert "API call failed." in emitted[0]["text"]
        assert "Observation queued" in result[0].text

    def test_user_context_still_queues_inbox_in_debug_mode(self, inbox_dir: Path):
        """user_context observations always write to inbox and emit an event."""
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append({"text": text, "event_type": event_type, "severity": severity, "emitter": emitter})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _emit_event=fake_emit,
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
        # Event also emitted via event bus for user_context
        assert len(emitted) == 1
        assert emitted[0]["event_type"] == "agent.observation.user_context"
        assert emitted[0]["severity"] == "info"
        assert "Observation queued" in result[0].text

    def test_system_context_goes_to_inbox_in_non_debug_mode(self, inbox_dir: Path):
        """system_context observations write to inbox when LOBSTER_DEBUG=false."""
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            category: str = "system_context",
            visibility: str = "mcp-only",
            emitter: str | None = None,
        ) -> None:
            emitted.append({"text": text, "category": category, "visibility": visibility, "emitter": emitter})

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

    def test_debug_mirror_includes_task_id_as_emitter(self, inbox_dir: Path):
        """task_id is passed as the emitter to _emit_event as 'task:<task_id>'."""
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append({"text": text, "event_type": event_type, "emitter": emitter})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _emit_event=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Something went wrong.",
                "category": "system_error",
                "task_id": "task-42",
            }))

        assert len(emitted) == 1
        assert emitted[0]["emitter"] == "task:task-42"
        assert emitted[0]["event_type"] == "agent.observation.system_error"

    def test_debug_bypass_includes_task_id_in_emitted_text(self, inbox_dir: Path):
        """The emitter passed to _emit_event contains the task_id prefix 'task:'."""
        emitted: list[dict] = []

        def fake_emit(
            text: str,
            event_type: str = "debug.observation",
            severity: str = "debug",
            source: str = "inbox-server",
            emitter: str | None = None,
            task_id: str | None = None,
            chat_id=None,
        ) -> None:
            emitted.append({"text": text, "emitter": emitter})

        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            _emit_event=fake_emit,
        ):
            from src.mcp.inbox_server import handle_write_observation
            asyncio.run(handle_write_observation({
                "chat_id": 123,
                "text": "Disk nearly full.",
                "category": "system_error",
                "task_id": "disk-check-99",
            }))

        assert len(emitted) == 1
        assert emitted[0]["emitter"] == "task:disk-check-99"


class TestHandleWriteObservationLogWrite:
    """Tests for the belt-and-suspenders direct observations.log write."""

    def _run_with_dirs(self, args: dict, inbox_dir: Path, log_dir: Path) -> list:
        with patch.multiple(
            "src.mcp.inbox_server",
            INBOX_DIR=inbox_dir,
            LOG_DIR=log_dir,
            _DEBUG_MODE=False,
            _DEBUG_RESOLVED=True,
        ):
            from src.mcp.inbox_server import handle_write_observation
            return asyncio.run(handle_write_observation(args))

    @pytest.fixture
    def inbox_dir(self, temp_messages_dir: Path) -> Path:
        return temp_messages_dir / "inbox"

    @pytest.fixture
    def log_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # system_error appends to observations.log
    # ------------------------------------------------------------------

    def test_system_error_appends_json_line_to_observations_log(
        self, inbox_dir: Path, log_dir: Path
    ):
        """system_error observation writes a JSON line directly to observations.log."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "API call failed unexpectedly.",
                "category": "system_error",
            },
            inbox_dir,
            log_dir,
        )

        obs_log = log_dir / "observations.log"
        assert obs_log.exists(), "observations.log should be created"
        lines = [ln for ln in obs_log.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["category"] == "system_error"
        assert entry["content"] == "API call failed unexpectedly."
        assert entry["source"] == "mcp-direct"
        assert "ts" in entry

    def test_system_error_log_entry_includes_task_id_when_provided(
        self, inbox_dir: Path, log_dir: Path
    ):
        """When task_id is supplied, the log entry includes it."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "DB connection lost.",
                "category": "system_error",
                "task_id": "task-db-99",
            },
            inbox_dir,
            log_dir,
        )

        obs_log = log_dir / "observations.log"
        entry = json.loads(obs_log.read_text().strip())
        assert entry.get("task_id") == "task-db-99"

    def test_system_error_log_entry_omits_task_id_when_absent(
        self, inbox_dir: Path, log_dir: Path
    ):
        """When task_id is not supplied, it is absent from the log entry."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "Unexpected state detected.",
                "category": "system_error",
            },
            inbox_dir,
            log_dir,
        )

        obs_log = log_dir / "observations.log"
        entry = json.loads(obs_log.read_text().strip())
        assert "task_id" not in entry

    def test_multiple_system_errors_append_multiple_lines(
        self, inbox_dir: Path, log_dir: Path
    ):
        """Successive system_error calls each append a new line (not overwrite)."""
        for i in range(3):
            self._run_with_dirs(
                {
                    "chat_id": 123,
                    "text": f"Error #{i}",
                    "category": "system_error",
                },
                inbox_dir,
                log_dir,
            )

        obs_log = log_dir / "observations.log"
        lines = [ln for ln in obs_log.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3
        texts = [json.loads(ln)["content"] for ln in lines]
        assert texts == ["Error #0", "Error #1", "Error #2"]

    # ------------------------------------------------------------------
    # Non-system_error categories do NOT write to observations.log
    # ------------------------------------------------------------------

    def test_user_context_does_not_write_to_observations_log(
        self, inbox_dir: Path, log_dir: Path
    ):
        """user_context observations skip the direct log write."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "User prefers dark mode.",
                "category": "user_context",
            },
            inbox_dir,
            log_dir,
        )

        obs_log = log_dir / "observations.log"
        assert not obs_log.exists(), "observations.log should not be created for user_context"

    def test_system_context_does_not_write_to_observations_log(
        self, inbox_dir: Path, log_dir: Path
    ):
        """system_context observations skip the direct log write."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "Config drift detected.",
                "category": "system_context",
            },
            inbox_dir,
            log_dir,
        )

        obs_log = log_dir / "observations.log"
        assert not obs_log.exists(), "observations.log should not be created for system_context"

    def test_system_error_also_writes_inbox_message(
        self, inbox_dir: Path, log_dir: Path
    ):
        """The log write is additive — the inbox message is still written for dispatcher routing."""
        self._run_with_dirs(
            {
                "chat_id": 123,
                "text": "Service unreachable.",
                "category": "system_error",
            },
            inbox_dir,
            log_dir,
        )

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 1, "Inbox message must still be written"
        content = json.loads(inbox_files[0].read_text())
        assert content["type"] == "subagent_observation"
        assert content["category"] == "system_error"

        obs_log = log_dir / "observations.log"
        assert obs_log.exists(), "observations.log must also be written"
