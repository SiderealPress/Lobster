"""
Unit tests for hooks/require-write-result.py

Tests cover:
- _extract_write_result_task_ids(): returns task_ids from write_result tool_use blocks
- _extract_write_result_task_ids(): handles empty input, missing fields, non-string values
- _extract_write_result_task_ids(): deduplicates and preserves order
- _load_transcript_from_jsonl(): reads JSONL transcript files
- _collect_tool_use_and_text(): handles JSONL format and legacy inline format
- _mark_session_notified(): best-effort notified_at update (swallows errors)
- main(): exits 0 when write_result is called with a valid chat_id
- main(): exits 2 when write_result is absent from the transcript
- main(): exits 2 when write_result is called but chat_id is None in every call
- main(): exits 2 with pseudocode-specific error when write_result appears only in text
- main(): exits 0 immediately for dispatcher sessions (exempt from check)
- main(): exits 0 when chat_id=0 (background-agent system route)
- main(): SubagentStop with no agent_transcript_path exits 0 (can't verify, allow)
"""

import importlib.util
import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_hook():
    """Load hooks/require-write-result.py as a module without executing main().

    Inserts the hooks dir onto sys.path so the hook's own imports work,
    then returns the loaded module object.
    """
    hooks_dir = Path(__file__).parent.parent / "hooks"
    hook_path = hooks_dir / "require-write-result.py"

    # The hook does `sys.path.insert(0, str(Path(__file__).parent))` at import
    # time to find session_role. We replicate that here so the import succeeds.
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))

    spec = importlib.util.spec_from_file_location("require_write_result", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_write_result_item(task_id=None, chat_id=12345):
    """Build a tool_use item that represents a write_result call."""
    inp: dict = {"text": "done", "status": "success"}
    if chat_id is not None:
        inp["chat_id"] = chat_id
    if task_id is not None:
        inp["task_id"] = task_id
    return {
        "type": "tool_use",
        "name": "mcp__lobster-inbox__write_result",
        "input": inp,
    }


def _make_other_tool_item(name="mcp__lobster-inbox__send_reply"):
    """Build a tool_use item for a tool other than write_result."""
    return {"type": "tool_use", "name": name, "input": {}}


def _inline_transcript(tool_use_items=None, text_items=None):
    """Build a legacy inline transcript (list of messages with content lists).

    This is the legacy format the hook reads from data["transcript"]:
        [{"role": "assistant", "content": [<tool_use or text item>, ...]}, ...]
    """
    content = list(tool_use_items or [])
    for t in (text_items or []):
        content.append({"type": "text", "text": t})
    return [{"role": "assistant", "content": content}]


def _jsonl_transcript(tool_use_items=None, text_items=None):
    """Build a JSONL-format transcript (CC 2.1.76+ style).

    Each entry has the structure:
        {"type": "assistant", "message": {"role": "assistant", "content": [...]}, ...}

    Tool use and text items are nested under entry["message"]["content"].
    """
    content = list(tool_use_items or [])
    for t in (text_items or []):
        content.append({"type": "text", "text": t})
    return [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": content},
        }
    ]


def _run_main(hook_mod, hook_data: dict):
    """Call hook_mod.main() with mocked stdin and return the SystemExit code.

    Patches _mark_session_completed and _mark_session_notified to avoid
    touching the real DB.
    """
    stdin_json = json.dumps(hook_data)
    with patch("sys.stdin", StringIO(stdin_json)), \
         patch.object(hook_mod, "_mark_session_completed"), \
         patch.object(hook_mod, "_mark_session_notified"):
        try:
            hook_mod.main()
        except SystemExit as e:
            return e.code
    return 0


# ---------------------------------------------------------------------------
# Tests: _extract_write_result_task_ids
# ---------------------------------------------------------------------------

class TestExtractWriteResultTaskIds:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_returns_task_id_when_present(self):
        """Given a write_result item with task_id, returns that task_id."""
        items = [_make_write_result_item(task_id="my-task-42")]
        result = self.mod._extract_write_result_task_ids(items)
        assert result == ["my-task-42"]

    def test_empty_list_returns_empty(self):
        """An empty tool_use list returns an empty list."""
        result = self.mod._extract_write_result_task_ids([])
        assert result == []

    def test_no_write_result_calls_returns_empty(self):
        """Items that are not write_result calls return an empty list."""
        items = [
            _make_other_tool_item(),
            _make_other_tool_item("mcp__github__issue_read"),
        ]
        result = self.mod._extract_write_result_task_ids(items)
        assert result == []

    def test_write_result_without_task_id_returns_empty(self):
        """A write_result call with no task_id field returns an empty list."""
        items = [_make_write_result_item(task_id=None)]
        result = self.mod._extract_write_result_task_ids(items)
        assert result == []

    def test_empty_string_task_id_is_excluded(self):
        """A write_result call with an empty-string task_id is excluded."""
        item = {
            "type": "tool_use",
            "name": "mcp__lobster-inbox__write_result",
            "input": {"task_id": "", "chat_id": 1},
        }
        result = self.mod._extract_write_result_task_ids([item])
        assert result == []

    def test_deduplicates_repeated_task_ids(self):
        """Duplicate task_ids from multiple write_result calls appear only once."""
        items = [
            _make_write_result_item(task_id="task-1"),
            _make_write_result_item(task_id="task-1"),
        ]
        result = self.mod._extract_write_result_task_ids(items)
        assert result == ["task-1"]

    def test_multiple_distinct_task_ids_preserved_in_order(self):
        """Multiple distinct task_ids are returned in encounter order."""
        items = [
            _make_write_result_item(task_id="alpha"),
            _make_other_tool_item(),
            _make_write_result_item(task_id="beta"),
        ]
        result = self.mod._extract_write_result_task_ids(items)
        assert result == ["alpha", "beta"]

    def test_non_string_task_id_is_excluded(self):
        """A non-string task_id (e.g. int) is treated as invalid and excluded."""
        item = {
            "type": "tool_use",
            "name": "mcp__lobster-inbox__write_result",
            "input": {"task_id": 42, "chat_id": 1},
        }
        result = self.mod._extract_write_result_task_ids([item])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _load_transcript_from_jsonl
# ---------------------------------------------------------------------------

class TestLoadTranscriptFromJsonl:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_loads_valid_jsonl_lines(self):
        """Parses each JSON line and returns a list of parsed objects."""
        lines = [
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": []}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": []}}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            path = f.name
        result = self.mod._load_transcript_from_jsonl(path)
        assert len(result) == 2
        assert result[0]["type"] == "assistant"
        assert result[1]["type"] == "user"

    def test_skips_blank_lines(self):
        """Blank lines in the JSONL file are silently skipped."""
        lines = [
            json.dumps({"type": "assistant", "message": {}}),
            "",
            json.dumps({"type": "user", "message": {}}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            path = f.name
        result = self.mod._load_transcript_from_jsonl(path)
        assert len(result) == 2

    def test_returns_empty_for_nonexistent_file(self):
        """A non-existent path returns an empty list (best-effort)."""
        result = self.mod._load_transcript_from_jsonl("/does/not/exist.jsonl")
        assert result == []

    def test_skips_invalid_json_lines(self):
        """Lines that are not valid JSON are skipped; valid lines are parsed."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"type": "assistant"}\n')
            f.write("not valid json\n")
            f.write('{"type": "user"}\n')
            path = f.name
        result = self.mod._load_transcript_from_jsonl(path)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Tests: _collect_tool_use_and_text
# ---------------------------------------------------------------------------

class TestCollectToolUseAndText:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_legacy_inline_format(self):
        """Extracts tool_use items from legacy inline format (content on message dict)."""
        transcript = _inline_transcript([_make_write_result_item(task_id="t1")])
        tools, texts = self.mod._collect_tool_use_and_text(transcript)
        assert len(tools) == 1
        assert tools[0]["name"] == "mcp__lobster-inbox__write_result"

    def test_jsonl_nested_format(self):
        """Extracts tool_use items from JSONL format (content under message.message.content)."""
        transcript = _jsonl_transcript([_make_write_result_item(task_id="t2")])
        tools, texts = self.mod._collect_tool_use_and_text(transcript)
        assert len(tools) == 1
        assert tools[0]["name"] == "mcp__lobster-inbox__write_result"

    def test_extracts_text_content_parts(self):
        """Text items are returned in the text_content_parts list."""
        transcript = _inline_transcript(text_items=["some text here"])
        tools, texts = self.mod._collect_tool_use_and_text(transcript)
        assert tools == []
        assert "some text here" in texts

    def test_empty_transcript_returns_empty(self):
        """An empty transcript returns empty lists for both tools and texts."""
        tools, texts = self.mod._collect_tool_use_and_text([])
        assert tools == []
        assert texts == []


# ---------------------------------------------------------------------------
# Tests: _mark_session_notified
# ---------------------------------------------------------------------------

class TestMarkSessionNotified:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_swallows_import_error(self):
        """_mark_session_notified does not raise even when session_store is unavailable."""
        # The function is best-effort and swallows all exceptions.
        # Force an import error by patching __import__ to raise for session_store.
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fail_import(name, *args, **kwargs):
            if "session_store" in name:
                raise ImportError("No module named 'agents.session_store'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_import):
            # Should not raise — best-effort function swallows all exceptions.
            self.mod._mark_session_notified("some-task-id")

    def test_calls_set_notified_when_available(self):
        """When session_store is importable, _mark_session_notified calls set_notified."""
        mock_set_notified = MagicMock()
        mock_session_store = MagicMock()
        mock_session_store.set_notified = mock_set_notified

        with patch.dict("sys.modules", {"agents.session_store": mock_session_store}):
            self.mod._mark_session_notified("task-abc")

        mock_set_notified.assert_called_once_with("task-abc")


# ---------------------------------------------------------------------------
# Tests: main() via stdin injection
# ---------------------------------------------------------------------------

class TestMainFlow:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_exits_0_when_write_result_called_with_valid_chat_id(self):
        """main() exits 0 when write_result is called with a valid non-None chat_id."""
        transcript = _inline_transcript([_make_write_result_item(chat_id=12345)])
        data = {"transcript": transcript}
        # The hook does `from session_role import is_dispatcher, get_session_id`
        # so both names are bound directly on the module; patch them there.
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-123"):
            code = _run_main(self.mod, data)
        assert code == 0

    def test_exits_2_when_write_result_absent(self):
        """main() exits 2 when write_result was not called at all."""
        transcript = _inline_transcript([_make_other_tool_item()])
        data = {"transcript": transcript}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-abc"):
            code = _run_main(self.mod, data)
        assert code == 2

    def test_exits_2_when_empty_transcript(self):
        """main() exits 2 for an empty transcript (no tools called at all)."""
        data = {"transcript": []}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-empty"):
            code = _run_main(self.mod, data)
        assert code == 2

    def test_exits_2_when_chat_id_is_none_in_all_write_result_calls(self):
        """main() exits 2 when write_result was called but chat_id is None every time."""
        # Build a write_result item with no chat_id key (input["chat_id"] absent → None check fails).
        item = {
            "type": "tool_use",
            "name": "mcp__lobster-inbox__write_result",
            "input": {"task_id": "t1", "text": "done"},  # no chat_id key
        }
        transcript = _inline_transcript([item])
        data = {"transcript": transcript}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-xyz"):
            code = _run_main(self.mod, data)
        assert code == 2

    def test_exits_0_when_chat_id_is_zero(self):
        """main() exits 0 when chat_id=0 (valid dispatcher system route)."""
        transcript = _inline_transcript([_make_write_result_item(chat_id=0)])
        data = {"transcript": transcript}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-bg"):
            code = _run_main(self.mod, data)
        assert code == 0

    def test_exits_0_for_dispatcher_session(self):
        """main() exits 0 immediately for dispatcher sessions (exempt from check)."""
        # Dispatcher has no write_result call — still allowed.
        transcript = _inline_transcript([_make_other_tool_item()])
        data = {"transcript": transcript}
        # The hook does `from session_role import is_dispatcher` so the name is
        # bound directly on the loaded module object; patch it there.
        with patch.object(self.mod, "is_dispatcher", return_value=True):
            code = _run_main(self.mod, data)
        assert code == 0

    def test_exits_2_with_pseudocode_message_on_stderr_when_write_result_in_text(self, capsys):
        """main() exits 2 and emits pseudocode-specific error to stderr when
        write_result appears in text output but was never called as a tool."""
        transcript = _inline_transcript(
            tool_use_items=[_make_other_tool_item()],
            text_items=["You should call mcp__lobster-inbox__write_result now."],
        )
        data = {"transcript": transcript}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-pseudo"):
            code = _run_main(self.mod, data)
        assert code == 2
        captured = capsys.readouterr()
        # The hook writes error messages to stderr.
        assert "described as text but not called as a tool" in captured.err

    def test_marks_session_completed_and_notified_with_task_id_on_success(self):
        """main() calls _mark_session_completed and _mark_session_notified with
        the write_result task_id on exit 0."""
        transcript = _inline_transcript(
            [_make_write_result_item(task_id="my-task", chat_id=42)]
        )
        data = {"transcript": transcript}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-mark"), \
             patch.object(self.mod, "_mark_session_completed") as mock_completed, \
             patch.object(self.mod, "_mark_session_notified") as mock_notified:
            try:
                stdin_json = json.dumps(data)
                with patch("sys.stdin", StringIO(stdin_json)):
                    self.mod.main()
            except SystemExit:
                pass
        completed_args = [c.args[0] for c in mock_completed.call_args_list]
        notified_args = [c.args[0] for c in mock_notified.call_args_list]
        assert "my-task" in completed_args
        assert "my-task" in notified_args

    def test_exits_2_when_missing_transcript_key(self):
        """main() exits 2 (no write_result) when transcript key is absent from data."""
        data = {}  # no "transcript" key — defaults to []
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-missing"):
            code = _run_main(self.mod, data)
        assert code == 2

    def test_exits_0_when_stdin_invalid_json(self):
        """main() exits 0 (allow) when stdin is not valid JSON."""
        with patch("sys.stdin", StringIO("not json at all")):
            try:
                self.mod.main()
            except SystemExit as e:
                code = e.code
            else:
                code = 0
        assert code == 0

    def test_subagentstop_exits_0_when_no_agent_transcript_path(self):
        """main() exits 0 for SubagentStop when agent_transcript_path is absent.

        When there is no path to verify, the hook cannot check the transcript
        and allows the exit rather than blocking on incomplete information.
        """
        data = {"hook_event_name": "SubagentStop"}  # no agent_transcript_path
        with patch.object(self.mod, "is_dispatcher", return_value=False):
            code = _run_main(self.mod, data)
        assert code == 0

    def test_subagentstop_reads_jsonl_transcript(self):
        """main() reads the JSONL file at agent_transcript_path for SubagentStop events."""
        # Build a JSONL transcript file containing a valid write_result call.
        jsonl_entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [_make_write_result_item(task_id="t-jsonl", chat_id=99)],
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps(jsonl_entry) + "\n")
            path = f.name

        data = {"hook_event_name": "SubagentStop", "agent_transcript_path": path}
        with patch.object(self.mod, "is_dispatcher", return_value=False), \
             patch.object(self.mod, "get_session_id", return_value="sess-jsonl"):
            code = _run_main(self.mod, data)
        assert code == 0
