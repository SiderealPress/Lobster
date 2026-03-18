"""
Unit tests for hooks/require-auditor-context-update.py

Tests cover:
- _load_transcript_from_jsonl(): reads JSONL transcript files correctly
- _extract_tool_calls(): handles CC 2.1.76+ JSONL format and legacy inline format
- _is_auditor_session(): detects auditor sessions via Read and Bash tool calls
- _safe_word_in_transcript(): detects AUDIT_CONTEXT_UNCHANGED in write_result calls
- _session_start_time(): reads timestamp from hook_input and loaded transcript
- main(): SubagentStop with agent_transcript_path — compliant session (exits 0)
- main(): SubagentStop with agent_transcript_path — non-compliant session (exits 2)
- main(): SubagentStop with no agent_transcript_path falls back to inline transcript
- main(): non-auditor session exits 0 without enforcing anything
- main(): safe word in write_result exits 0
- main(): missing transcript exits 0 for non-auditor (no enforcement)
- main(): inline legacy transcript still works (backwards compatibility)
"""

import importlib.util
import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook():
    """Load hooks/require-auditor-context-update.py as a module without executing main()."""
    hooks_dir = Path(__file__).parent.parent / "hooks"
    hook_path = hooks_dir / "require-auditor-context-update.py"

    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))

    spec = importlib.util.spec_from_file_location(
        "require_auditor_context_update", hook_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Transcript builders
# ---------------------------------------------------------------------------

def _make_read_tool_call(file_path: str) -> dict:
    """Build a tool_use item representing a Read tool call."""
    return {
        "type": "tool_use",
        "name": "Read",
        "input": {"file_path": file_path},
    }


def _make_bash_tool_call(command: str) -> dict:
    """Build a tool_use item representing a Bash tool call."""
    return {
        "type": "tool_use",
        "name": "Bash",
        "input": {"command": command},
    }


def _make_write_result_call(text: str = "done", chat_id: int = 12345) -> dict:
    """Build a tool_use item representing a write_result call."""
    return {
        "type": "tool_use",
        "name": "mcp__lobster-inbox__write_result",
        "input": {"text": text, "chat_id": chat_id, "task_id": "auditor-task"},
    }


def _jsonl_entry(tool_use_items: list) -> dict:
    """Build a single JSONL transcript entry (CC 2.1.76+ format)."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": tool_use_items,
        },
    }


def _inline_entry(tool_use_items: list) -> dict:
    """Build a single legacy inline transcript entry."""
    return {
        "role": "assistant",
        "content": tool_use_items,
    }


def _write_jsonl_transcript(tmp_path: Path, entries: list) -> str:
    """Write a list of transcript entries to a JSONL file and return the path."""
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Helper: run main() with mocked stdin
# ---------------------------------------------------------------------------

def _run_main(hook_mod, hook_data: dict) -> int:
    """Call hook_mod.main() with mocked stdin. Returns the SystemExit code."""
    stdin_json = json.dumps(hook_data)
    with patch("sys.stdin", StringIO(stdin_json)):
        try:
            hook_mod.main()
        except SystemExit as e:
            return e.code
    return 0


# ---------------------------------------------------------------------------
# Tests: _load_transcript_from_jsonl
# ---------------------------------------------------------------------------

class TestLoadTranscriptFromJsonl:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_loads_valid_jsonl(self, tmp_path):
        """Parses each JSON line and returns a list of parsed objects."""
        entries = [
            {"type": "assistant", "message": {"role": "assistant", "content": []}},
            {"type": "user", "message": {"role": "user", "content": []}},
        ]
        path = _write_jsonl_transcript(tmp_path, entries)
        result = self.mod._load_transcript_from_jsonl(path)
        assert len(result) == 2
        assert result[0]["type"] == "assistant"

    def test_skips_blank_lines(self, tmp_path):
        """Blank lines are silently skipped."""
        path = tmp_path / "transcript.jsonl"
        path.write_text(
            '{"type": "assistant"}\n\n{"type": "user"}\n'
        )
        result = self.mod._load_transcript_from_jsonl(str(path))
        assert len(result) == 2

    def test_returns_empty_for_nonexistent_file(self):
        """A non-existent path returns an empty list."""
        result = self.mod._load_transcript_from_jsonl("/does/not/exist.jsonl")
        assert result == []

    def test_skips_invalid_json_lines(self, tmp_path):
        """Invalid JSON lines are skipped; valid lines are parsed."""
        path = tmp_path / "transcript.jsonl"
        path.write_text('{"type": "assistant"}\nnot json\n{"type": "user"}\n')
        result = self.mod._load_transcript_from_jsonl(str(path))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Tests: _extract_tool_calls (JSONL and legacy formats)
# ---------------------------------------------------------------------------

class TestExtractToolCalls:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_extracts_from_jsonl_nested_format(self):
        """Tool calls are extracted from JSONL format (content under message.content)."""
        read_call = _make_read_tool_call("/path/to/system-audit.context.md")
        transcript = [_jsonl_entry([read_call])]
        result = self.mod._extract_tool_calls(transcript)
        assert len(result) == 1
        assert result[0]["name"] == "Read"

    def test_extracts_from_legacy_inline_format(self):
        """Tool calls are extracted from legacy inline format (content on entry)."""
        read_call = _make_read_tool_call("/path/to/system-audit.context.md")
        transcript = [_inline_entry([read_call])]
        result = self.mod._extract_tool_calls(transcript)
        assert len(result) == 1
        assert result[0]["name"] == "Read"

    def test_returns_empty_for_empty_transcript(self):
        """An empty transcript returns an empty list."""
        result = self.mod._extract_tool_calls([])
        assert result == []

    def test_skips_non_tool_use_items(self):
        """Text content items are not included in tool_calls output."""
        entry = _jsonl_entry([{"type": "text", "text": "hello"}])
        result = self.mod._extract_tool_calls([entry])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _is_auditor_session
# ---------------------------------------------------------------------------

class TestIsAuditorSession:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_detects_read_of_audit_context_file(self):
        """Returns True when a Read call references system-audit.context.md."""
        calls = [_make_read_tool_call("/home/user/lobster-user-config/agents/system-audit.context.md")]
        assert self.mod._is_auditor_session(calls) is True

    def test_detects_bash_referencing_audit_context_file(self):
        """Returns True when a Bash call references system-audit.context.md."""
        calls = [_make_bash_tool_call("cat ~/lobster-user-config/agents/system-audit.context.md")]
        assert self.mod._is_auditor_session(calls) is True

    def test_returns_false_for_unrelated_tool_calls(self):
        """Returns False when no tool call references system-audit.context.md."""
        calls = [
            _make_read_tool_call("/home/user/some-other-file.md"),
            _make_bash_tool_call("ls /tmp"),
        ]
        assert self.mod._is_auditor_session(calls) is False

    def test_returns_false_for_empty_list(self):
        """Returns False for an empty tool call list."""
        assert self.mod._is_auditor_session([]) is False


# ---------------------------------------------------------------------------
# Tests: _safe_word_in_transcript
# ---------------------------------------------------------------------------

class TestSafeWordInTranscript:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_detects_safe_word_in_write_result(self):
        """Returns True when write_result text contains AUDIT_CONTEXT_UNCHANGED."""
        calls = [_make_write_result_call(text="AUDIT_CONTEXT_UNCHANGED\nNothing new found.")]
        assert self.mod._safe_word_in_transcript(calls) is True

    def test_returns_false_when_safe_word_absent(self):
        """Returns False when write_result text does not contain the safe word."""
        calls = [_make_write_result_call(text="Found some issues.")]
        assert self.mod._safe_word_in_transcript(calls) is False

    def test_returns_false_for_non_write_result_calls(self):
        """Returns False when no write_result tool call is present."""
        calls = [_make_read_tool_call("/some/file.md")]
        assert self.mod._safe_word_in_transcript(calls) is False

    def test_returns_false_for_empty_list(self):
        """Returns False for an empty tool call list."""
        assert self.mod._safe_word_in_transcript([]) is False


# ---------------------------------------------------------------------------
# Tests: _session_start_time
# ---------------------------------------------------------------------------

class TestSessionStartTime:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_returns_session_start_time_from_hook_input(self):
        """Reads session_start_time directly from hook_input when present."""
        hook_input = {"session_start_time": 1700000000.0}
        result = self.mod._session_start_time(hook_input, [])
        assert result == 1700000000.0

    def test_returns_timestamp_from_hook_input_fallback(self):
        """Reads timestamp key from hook_input as fallback."""
        hook_input = {"timestamp": "1700000001.5"}
        result = self.mod._session_start_time(hook_input, [])
        assert result == 1700000001.5

    def test_reads_min_timestamp_from_transcript(self):
        """When hook_input has no timestamp, finds the minimum timestamp in transcript."""
        transcript = [
            {"type": "user", "timestamp": 1700000010.0},
            {"type": "assistant", "timestamp": 1700000005.0},
        ]
        result = self.mod._session_start_time({}, transcript)
        assert result == 1700000005.0

    def test_returns_none_when_no_timestamp_available(self):
        """Returns None when neither hook_input nor transcript have timestamps."""
        result = self.mod._session_start_time({}, [])
        assert result is None


# ---------------------------------------------------------------------------
# Tests: main() — SubagentStop with agent_transcript_path (CC 2.1.76+)
# ---------------------------------------------------------------------------

class TestMainSubagentStop:
    @pytest.fixture(autouse=True)
    def _mod(self):
        self.mod = _load_hook()

    def test_compliant_session_exits_0_with_safe_word(self, tmp_path):
        """Exits 0 when auditor session includes AUDIT_CONTEXT_UNCHANGED in write_result."""
        # Build a JSONL transcript: auditor reads context file, then writes safe word
        read_call = _make_read_tool_call("/home/user/lobster-user-config/agents/system-audit.context.md")
        write_call = _make_write_result_call(text="AUDIT_CONTEXT_UNCHANGED\nNothing new.")

        entries = [
            _jsonl_entry([read_call]),
            _jsonl_entry([write_call]),
        ]
        path = _write_jsonl_transcript(tmp_path, entries)

        hook_data = {
            "hook_event_name": "SubagentStop",
            "agent_transcript_path": path,
        }
        code = _run_main(self.mod, hook_data)
        assert code == 0

    def test_non_compliant_session_exits_2(self, tmp_path):
        """Exits 2 when auditor session has no safe word and context file not updated."""
        # Build a JSONL transcript: auditor reads context file but does NOT call
        # write_result with the safe word and does NOT update the context file.
        read_call = _make_read_tool_call("/home/user/lobster-user-config/agents/system-audit.context.md")
        # write_result exists but without the safe word
        write_call = _make_write_result_call(text="Audit complete, found issues.")

        entries = [
            _jsonl_entry([read_call]),
            _jsonl_entry([write_call]),
        ]
        path = _write_jsonl_transcript(tmp_path, entries)

        # Patch _context_file_updated_since to return False (file not updated)
        hook_data = {
            "hook_event_name": "SubagentStop",
            "agent_transcript_path": path,
        }
        with patch.object(self.mod, "_context_file_updated_since", return_value=False):
            code = _run_main(self.mod, hook_data)
        assert code == 2

    def test_non_auditor_session_exits_0(self, tmp_path):
        """Exits 0 immediately for non-auditor sessions (no enforcement)."""
        # Transcript has tool calls but none reference system-audit.context.md
        bash_call = _make_bash_tool_call("ls /tmp")
        entries = [_jsonl_entry([bash_call])]
        path = _write_jsonl_transcript(tmp_path, entries)

        hook_data = {
            "hook_event_name": "SubagentStop",
            "agent_transcript_path": path,
        }
        code = _run_main(self.mod, hook_data)
        assert code == 0

    def test_no_agent_transcript_path_falls_back_to_inline(self):
        """Falls back to inline transcript when agent_transcript_path is absent."""
        # Legacy inline transcript: auditor reads context file — but no safe word
        # and we force context-file-not-updated → should block.
        read_call = _make_read_tool_call("/home/user/lobster-user-config/agents/system-audit.context.md")
        inline_transcript = [_inline_entry([read_call])]

        hook_data = {
            "hook_event_name": "SubagentStop",
            # No agent_transcript_path — uses inline transcript
            "transcript": inline_transcript,
        }
        with patch.object(self.mod, "_context_file_updated_since", return_value=False):
            code = _run_main(self.mod, hook_data)
        # Auditor session detected but no safe word and file not updated → block
        assert code == 2

    def test_empty_agent_transcript_path_falls_back_to_inline(self):
        """Falls back to inline transcript when agent_transcript_path is empty string."""
        # Non-auditor inline transcript — should pass through.
        inline_transcript = [_inline_entry([_make_bash_tool_call("echo hello")])]
        hook_data = {
            "agent_transcript_path": "",
            "transcript": inline_transcript,
        }
        code = _run_main(self.mod, hook_data)
        assert code == 0

    def test_context_file_updated_exits_0(self, tmp_path):
        """Exits 0 when context file was updated during the session (condition 1)."""
        read_call = _make_read_tool_call("/home/user/lobster-user-config/agents/system-audit.context.md")
        # No safe word in write_result — but the file was updated
        write_call = _make_write_result_call(text="Updated the context file.")
        entries = [_jsonl_entry([read_call, write_call])]
        path = _write_jsonl_transcript(tmp_path, entries)

        hook_data = {
            "hook_event_name": "SubagentStop",
            "agent_transcript_path": path,
        }
        # Pretend the context file was updated during this session
        with patch.object(self.mod, "_context_file_updated_since", return_value=True):
            code = _run_main(self.mod, hook_data)
        assert code == 0

    def test_invalid_stdin_exits_0(self):
        """Exits 0 when stdin is not valid JSON (never block on bad input)."""
        with patch("sys.stdin", StringIO("not json at all")):
            try:
                self.mod.main()
            except SystemExit as e:
                code = e.code
            else:
                code = 0
        assert code == 0
