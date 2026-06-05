"""
Unit tests for hooks/context-monitor.py (issue #2056: remove wind-down mode).

The context monitor reports token counts to a log. It does NOT trigger wind-down
mode, does NOT write context_warning inbox messages, and does NOT call any state
machine transitions. CC handles compaction on its own; no Lobster-side preparation
is needed.

Behaviors verified:
1. Transcript present with usage → correct percentage computed from token counts.
2. transcript_path absent → WARN logged, no crash.
3. Last assistant turn is selected when multiple turns exist.
4. Model lookup table: Sonnet 4.6 = 200k (CC default), Haiku 4.5 = 200k, unknown = 200k.
5. Token usage is always logged (no threshold suppression).
6. At ANY usage level, no context_warning is written to inbox.
7. _handle_payload() accepts injectable log_dir and inbox_dir.
8. Wind-down artifacts (WARNING_THRESHOLD, DEDUP_FLAG, _write_winding_down, etc.) are absent.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "context-monitor.py"

# Named constants matching the spec — these are protocol-level values.
WARN_PREFIX_ABSENT_CONTEXT = "[WARN] transcript usage unavailable"
# claude-sonnet-4-6 supports up to 1M tokens but CC's default window is 200k.
# Update when we can detect which mode is active.
SONNET_4_6_MAX_CONTEXT = 200_000
# claude-opus-4-6 also uses CC's default 200k window.
OPUS_4_6_MAX_CONTEXT = 200_000
HAIKU_4_5_MAX_CONTEXT = 200_000
DEFAULT_MAX_CONTEXT = 200_000


def _load_hook():
    """Load context-monitor as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("context_monitor", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_log(log_dir: Path) -> list[dict]:
    log_file = log_dir / "context-monitor.log"
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]


def _make_transcript(tmp_path: Path, turns: list[dict]) -> Path:
    """Write a transcript JSONL file with the given assistant turns.

    Each turn dict should contain at least 'model' and 'usage'.
    The JSONL format wraps each turn as:
      {"type": "assistant", "message": {"role": "assistant", "model": ..., "usage": ...}}
    """
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for turn in turns:
            obj = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": turn.get("model", "claude-sonnet-4-6"),
                    "usage": turn.get("usage", {}),
                },
            }
            f.write(json.dumps(obj) + "\n")
    return path


class TestTranscriptUsageReading:
    """_read_transcript_usage() reads the last assistant turn's token counts."""

    def test_returns_correct_percentage_from_transcript(self, tmp_path):
        """Transcript with usage block → percentage computed from token sum / model max."""
        mod = _load_hook()
        # 100_000 tokens on a 200k-context Sonnet model (CC default) → 50%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 20_000,
                    "cache_creation_input_tokens": 40_000,
                    "cache_read_input_tokens": 40_000,
                    "output_tokens": 5_000,
                },
            }
        ])
        result = mod._read_transcript_usage(str(transcript))
        assert result is not None, "Expected usage data from transcript"
        used_pct, remaining_pct, model, total_tokens = result
        assert abs(used_pct - 50.0) < 0.01, f"Expected 50% used, got {used_pct}"
        assert abs(remaining_pct - 50.0) < 0.01
        assert model == "claude-sonnet-4-6"
        assert total_tokens == 100_000, f"Expected 100_000 raw tokens, got {total_tokens}"

    def test_last_turn_wins_when_multiple_turns_exist(self, tmp_path):
        """When multiple assistant turns exist, the last one's usage is returned."""
        mod = _load_hook()
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 20_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 160_000,  # 80% of 200k CC window — this is the last turn
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        ])
        result = mod._read_transcript_usage(str(transcript))
        assert result is not None
        used_pct, _, _, total_tokens = result
        assert abs(used_pct - 80.0) < 0.01, (
            f"Expected 80% from last turn, got {used_pct}"
        )
        assert total_tokens == 160_000, f"Expected 160_000 raw tokens from last turn, got {total_tokens}"

    def test_returns_none_when_transcript_path_is_none(self, tmp_path):
        """No transcript_path → returns None (caller logs WARN)."""
        mod = _load_hook()
        result = mod._read_transcript_usage(None)
        assert result is None

    def test_returns_none_when_transcript_file_missing(self, tmp_path):
        """Nonexistent transcript path → returns None without crashing."""
        mod = _load_hook()
        result = mod._read_transcript_usage(str(tmp_path / "no-such-file.jsonl"))
        assert result is None

    def test_returns_none_when_no_assistant_turns(self, tmp_path):
        """Transcript with no assistant turns (e.g. only user turns) → None."""
        mod = _load_hook()
        path = tmp_path / "transcript.jsonl"
        # Write a user turn only — no assistant entry
        path.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})
            + "\n"
        )
        result = mod._read_transcript_usage(str(path))
        assert result is None

    def test_sums_all_cache_fields(self, tmp_path):
        """Total = input_tokens + cache_creation_input_tokens + cache_read_input_tokens."""
        mod = _load_hook()
        # Haiku 200k model: 100k + 40k + 60k = 200k = 100%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-haiku-4-5",
                "usage": {
                    "input_tokens": 100_000,
                    "cache_creation_input_tokens": 40_000,
                    "cache_read_input_tokens": 60_000,
                    "output_tokens": 500,
                },
            }
        ])
        result = mod._read_transcript_usage(str(transcript))
        assert result is not None
        used_pct, _, model, total_tokens = result
        assert abs(used_pct - 100.0) < 0.01, f"Expected 100%, got {used_pct}"
        assert model == "claude-haiku-4-5"
        assert total_tokens == 200_000, f"Expected 200_000 raw tokens, got {total_tokens}"


class TestModelContextLookup:
    """_model_max_context() returns correct sizes for known and unknown models."""

    def test_sonnet_4_6_returns_200k(self):
        """claude-sonnet-4-6 → 200_000 (CC default window)."""
        mod = _load_hook()
        assert mod._model_max_context("claude-sonnet-4-6") == SONNET_4_6_MAX_CONTEXT

    def test_opus_4_6_returns_200k(self):
        """claude-opus-4-6 → 200_000 (CC default window)."""
        mod = _load_hook()
        assert mod._model_max_context("claude-opus-4-6") == OPUS_4_6_MAX_CONTEXT

    def test_haiku_4_5_bare_returns_200k(self):
        """claude-haiku-4-5 → 200_000."""
        mod = _load_hook()
        assert mod._model_max_context("claude-haiku-4-5") == HAIKU_4_5_MAX_CONTEXT

    def test_haiku_4_5_versioned_returns_200k(self):
        """claude-haiku-4-5-20251001 (versioned suffix) → 200_000."""
        mod = _load_hook()
        assert mod._model_max_context("claude-haiku-4-5-20251001") == HAIKU_4_5_MAX_CONTEXT

    def test_unknown_model_returns_default(self):
        """Unrecognized model string → DEFAULT_CONTEXT_SIZE (conservative fallback)."""
        mod = _load_hook()
        assert mod._model_max_context("claude-future-model-99") == DEFAULT_MAX_CONTEXT

    def test_empty_model_returns_default(self):
        """Empty model string → DEFAULT_CONTEXT_SIZE."""
        mod = _load_hook()
        assert mod._model_max_context("") == DEFAULT_MAX_CONTEXT


class TestHandlePayloadLogging:
    """_handle_payload() logs token usage but never writes context_warning inbox messages."""

    def test_logs_usage_from_transcript_at_any_level(self, tmp_path):
        """Transcript present at 30% → usage entry logged with source=transcript_jsonl."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        # 60k / 200k (CC default) = 30%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 30_000,
                    "cache_creation_input_tokens": 20_000,
                    "cache_read_input_tokens": 10_000,
                },
            }
        ])
        payload = {
            "tool_name": "Bash",
            "transcript_path": str(transcript),
        }
        mod._handle_payload(payload, log_dir=log_dir)

        entries = _read_log(log_dir)
        assert len(entries) == 1
        entry = entries[0]
        assert abs(entry["used_percentage"] - 30.0) < 0.01
        assert entry.get("source") == "transcript_jsonl"
        assert not entry.get("transcript_unavailable", False)

    def test_logs_usage_above_former_threshold(self, tmp_path):
        """Transcript usage above 70% (old threshold) → usage logged, NO inbox message."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        # 160k / 200k = 80% — formerly above the wind-down threshold
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 160_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        ])
        payload = {
            "tool_name": "mcp__lobster-inbox__wait_for_messages",
            "transcript_path": str(transcript),
        }
        mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)

        # Token usage must still be logged
        entries = _read_log(log_dir)
        assert len(entries) == 1, f"Expected 1 log entry, got {len(entries)}"
        assert abs(entries[0]["used_percentage"] - 80.0) < 0.01

        # Inbox must be completely empty — no context_warning ever
        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 0, (
            f"context_warning must never be written to inbox (wind-down mode removed), "
            f"but found: {inbox_files}"
        )

    def test_no_inbox_message_at_full_context(self, tmp_path):
        """Even at 100% context usage, no context_warning is written to inbox."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        # 200k / 200k = 100% — maximum possible context usage
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 200_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        ])
        payload = {
            "tool_name": "Bash",
            "transcript_path": str(transcript),
        }
        mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 0, (
            f"Inbox must remain empty regardless of context level, but found: {inbox_files}"
        )

    def test_logs_warn_when_transcript_path_absent(self, tmp_path):
        """Payload with no transcript_path → WARN written to log, no crash."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        payload = {"tool_name": "mcp__lobster-inbox__wait_for_messages"}
        mod._handle_payload(payload, log_dir=log_dir)

        entries = _read_log(log_dir)
        assert len(entries) == 1, f"Expected 1 warn entry, got {len(entries)}: {entries}"
        entry = entries[0]
        assert entry.get("transcript_unavailable") is True
        assert WARN_PREFIX_ABSENT_CONTEXT in entry.get("warn", ""), (
            f"Expected warn prefix in entry, got: {entry}"
        )
        assert entry.get("tool") == "mcp__lobster-inbox__wait_for_messages"

    def test_no_inbox_message_when_transcript_absent(self, tmp_path):
        """Missing transcript_path must never trigger any inbox message."""
        mod = _load_hook()
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        payload = {"tool_name": "mcp__lobster-inbox__mark_processed"}
        mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 0, (
            f"No inbox message should ever be written, but found: {inbox_files}"
        )


class TestWindDownRemoved:
    """Wind-down mode artifacts are completely absent from the hook (issue #2056).

    These tests verify that the hook no longer contains the wind-down triggering
    mechanism: no WARNING_THRESHOLD, no DEDUP_FLAG, no _write_winding_down(),
    no state machine imports.
    """

    def test_no_warning_threshold_constant(self):
        """Hook must not export WARNING_THRESHOLD (removed with wind-down mode)."""
        mod = _load_hook()
        assert not hasattr(mod, "WARNING_THRESHOLD"), (
            "WARNING_THRESHOLD constant must be removed — wind-down mode is gone"
        )

    def test_no_dedup_flag_constant(self):
        """Hook must not export DEDUP_FLAG (removed with wind-down mode)."""
        mod = _load_hook()
        assert not hasattr(mod, "DEDUP_FLAG"), (
            "DEDUP_FLAG must be removed — wind-down dedup logic is gone"
        )

    def test_no_write_winding_down_function(self):
        """Hook must not export _write_winding_down() (removed with wind-down mode)."""
        mod = _load_hook()
        assert not hasattr(mod, "_write_winding_down"), (
            "_write_winding_down() must be removed — state machine transition is gone"
        )

    def test_no_build_warning_message_function(self):
        """Hook must not export _build_warning_message() (removed with wind-down mode)."""
        mod = _load_hook()
        assert not hasattr(mod, "_build_warning_message"), (
            "_build_warning_message() must be removed — inbox warning is gone"
        )

    def test_no_write_warning_to_inbox_function(self):
        """Hook must not export _write_warning_to_inbox() (removed with wind-down mode)."""
        mod = _load_hook()
        assert not hasattr(mod, "_write_warning_to_inbox"), (
            "_write_warning_to_inbox() must be removed — inbox warning is gone"
        )


class TestHandlePayloadSignature:
    """_handle_payload() must accept injectable paths for testability."""

    def test_handle_payload_accepts_log_dir_kwarg(self, tmp_path):
        """_handle_payload() must accept a log_dir keyword argument."""
        mod = _load_hook()
        import inspect
        sig = inspect.signature(mod._handle_payload)
        assert "log_dir" in sig.parameters, (
            "_handle_payload() must accept log_dir= for testability"
        )

    def test_handle_payload_accepts_inbox_dir_kwarg(self, tmp_path):
        """_handle_payload() must accept an inbox_dir keyword argument."""
        mod = _load_hook()
        import inspect
        sig = inspect.signature(mod._handle_payload)
        assert "inbox_dir" in sig.parameters, (
            "_handle_payload() must accept inbox_dir= for testability"
        )


# Named constants for the matcher pattern (issue #1985).
# The Claude Code hook matcher treats each |-separated segment as an exact tool
# name match. Adding .* to the MCP segment makes it a prefix match covering all
# mcp__lobster-inbox__* tools.
CONTEXT_MONITOR_MATCHER = "Bash|mcp__lobster-inbox__.*|Agent"
# The broken matcher that shipped before the fix — kept here so the regression
# test is self-documenting about what we are protecting against.
_BROKEN_MATCHER = "Bash|mcp__lobster-inbox__|Agent"


class TestSettingsMatcherPattern:
    """Verify the context-monitor matcher in settings.json covers MCP tool calls.

    The Claude Code hook runner matches each |-separated segment as a literal
    prefix/exact pattern. The segment 'mcp__lobster-inbox__' (without .*) is
    treated as an exact tool name — no real tool is named exactly that, so the
    hook silently never fires on any MCP call (issue #1985).

    These tests verify:
    1. The pattern "mcp__lobster-inbox__.*" matches every mcp__lobster-inbox__* tool.
    2. The broken pattern "mcp__lobster-inbox__" does NOT match any real tool name.
    3. The full CONTEXT_MONITOR_MATCHER covers Bash, Agent, and all MCP tools.
    4. settings.json contains the correct (fixed) matcher.
    """

    # Representative sample of real MCP tool names from the lobster-inbox server.
    REAL_MCP_TOOLS = [
        "mcp__lobster-inbox__wait_for_messages",
        "mcp__lobster-inbox__send_reply",
        "mcp__lobster-inbox__mark_processed",
        "mcp__lobster-inbox__mark_processing",
        "mcp__lobster-inbox__write_result",
        "mcp__lobster-inbox__check_inbox",
    ]

    def _segment_matches(self, pattern_segment: str, tool_name: str) -> bool:
        """Return True if the tool_name matches the pattern_segment as a regex."""
        import re
        return bool(re.fullmatch(pattern_segment, tool_name))

    def test_fixed_mcp_segment_matches_all_real_mcp_tools(self):
        """The fixed segment 'mcp__lobster-inbox__.*' matches every real MCP tool."""
        mcp_segment = "mcp__lobster-inbox__.*"
        for tool in self.REAL_MCP_TOOLS:
            assert self._segment_matches(mcp_segment, tool), (
                f"Fixed segment '{mcp_segment}' must match tool '{tool}' but did not"
            )

    def test_broken_mcp_segment_matches_no_real_mcp_tools(self):
        """The broken segment 'mcp__lobster-inbox__' (no .*) matches NO real MCP tool.

        This is the regression — the broken matcher silently skipped all MCP calls.
        """
        broken_segment = "mcp__lobster-inbox__"
        for tool in self.REAL_MCP_TOOLS:
            assert not self._segment_matches(broken_segment, tool), (
                f"Broken segment '{broken_segment}' unexpectedly matched '{tool}' — "
                "this confirms the pre-fix hook was broken"
            )

    def test_full_matcher_covers_bash_and_agent(self):
        """The full CONTEXT_MONITOR_MATCHER also matches Bash and Agent tools."""
        import re
        matcher_segments = CONTEXT_MONITOR_MATCHER.split("|")
        bash_matches = any(re.fullmatch(seg, "Bash") for seg in matcher_segments)
        agent_matches = any(re.fullmatch(seg, "Agent") for seg in matcher_segments)
        assert bash_matches, f"Matcher '{CONTEXT_MONITOR_MATCHER}' must match 'Bash'"
        assert agent_matches, f"Matcher '{CONTEXT_MONITOR_MATCHER}' must match 'Agent'"

    def test_full_matcher_covers_all_real_mcp_tools(self):
        """The full CONTEXT_MONITOR_MATCHER matches every real MCP tool via prefix."""
        import re
        matcher_segments = CONTEXT_MONITOR_MATCHER.split("|")
        for tool in self.REAL_MCP_TOOLS:
            matched = any(re.fullmatch(seg, tool) for seg in matcher_segments)
            assert matched, (
                f"CONTEXT_MONITOR_MATCHER '{CONTEXT_MONITOR_MATCHER}' "
                f"must match tool '{tool}' but did not"
            )

    def test_settings_json_uses_fixed_matcher(self):
        """settings.json contains the fixed matcher (mcp__lobster-inbox__.*).

        Reads ~/.claude/settings.json and confirms the context-monitor PostToolUse
        entry uses the corrected pattern, not the broken exact-match pattern.
        """
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            pytest.skip("~/.claude/settings.json not present in this environment")

        settings = json.loads(settings_path.read_text())
        posttool_hooks = settings.get("hooks", {}).get("PostToolUse", [])

        # Find the context-monitor entry
        context_monitor_entry = None
        for entry in posttool_hooks:
            hooks = entry.get("hooks", [])
            if any("context-monitor" in h.get("command", "") for h in hooks):
                context_monitor_entry = entry
                break

        if context_monitor_entry is None:
            pytest.skip("context-monitor hook not installed in this environment")

        matcher = context_monitor_entry.get("matcher", "")
        assert "mcp__lobster-inbox__.*" in matcher, (
            f"context-monitor matcher '{matcher}' must contain 'mcp__lobster-inbox__.*' "
            f"(not the broken 'mcp__lobster-inbox__' exact match)"
        )
