"""
Unit tests for hooks/context-monitor.py (issue #1790).

Root cause: The hook called data.get('context_window') on the PostToolUse payload,
which Claude Code never populates for PostToolUse events. The hook was a no-op in
every session.

Fix: Read actual token usage from the transcript JSONL file (at transcript_path in
the payload). The last assistant turn's usage block gives input + cache counts, which
are divided by the model's known max context to produce used_pct.

Behaviors verified:
1. Transcript present with usage → correct percentage computed from token counts.
2. transcript_path absent → WARN logged, no crash.
3. Last assistant turn is selected when multiple turns exist.
4. Model lookup table: Sonnet 4.6 = 1M, Haiku 4.5 = 200k, unknown = 200k.
5. At or above WARNING_THRESHOLD → context_warning written to inbox (once per session).
6. Dedup flag suppresses second warning.
7. _handle_payload() accepts injectable log_dir and inbox_dir.
"""

import importlib.util
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

_HOOKS_DIR = Path(__file__).parents[3] / "hooks"
_HOOK_PATH = _HOOKS_DIR / "context-monitor.py"

# Named constants matching the spec — these are protocol-level values.
WARN_PREFIX_ABSENT_CONTEXT = "[WARN] transcript usage unavailable"
WARNING_THRESHOLD = 70.0
SONNET_4_6_MAX_CONTEXT = 1_000_000
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
        # 500_000 tokens on a 1M-context Sonnet model → 50%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100_000,
                    "cache_creation_input_tokens": 200_000,
                    "cache_read_input_tokens": 200_000,
                    "output_tokens": 5_000,
                },
            }
        ])
        result = mod._read_transcript_usage(str(transcript))
        assert result is not None, "Expected usage data from transcript"
        used_pct, remaining_pct, model = result
        assert abs(used_pct - 50.0) < 0.01, f"Expected 50% used, got {used_pct}"
        assert abs(remaining_pct - 50.0) < 0.01
        assert model == "claude-sonnet-4-6"

    def test_last_turn_wins_when_multiple_turns_exist(self, tmp_path):
        """When multiple assistant turns exist, the last one's usage is returned."""
        mod = _load_hook()
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 800_000,  # 80% — this is the last turn
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        ])
        result = mod._read_transcript_usage(str(transcript))
        assert result is not None
        used_pct, _, _ = result
        assert abs(used_pct - 80.0) < 0.01, (
            f"Expected 80% from last turn, got {used_pct}"
        )

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
        used_pct, _, model = result
        assert abs(used_pct - 100.0) < 0.01, f"Expected 100%, got {used_pct}"
        assert model == "claude-haiku-4-5"


class TestModelContextLookup:
    """_model_max_context() returns correct sizes for known and unknown models."""

    def test_sonnet_4_6_returns_1m(self):
        """claude-sonnet-4-6 → 1_000_000."""
        mod = _load_hook()
        assert mod._model_max_context("claude-sonnet-4-6") == SONNET_4_6_MAX_CONTEXT

    def test_opus_4_6_returns_1m(self):
        """claude-opus-4-6 → 1_000_000."""
        mod = _load_hook()
        assert mod._model_max_context("claude-opus-4-6") == SONNET_4_6_MAX_CONTEXT

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


class TestHandlePayloadTranscriptPath:
    """_handle_payload() uses transcript_path to read usage from the JSONL."""

    def test_logs_usage_from_transcript_below_threshold(self, tmp_path):
        """Transcript present, below 70% → usage entry logged with source=transcript_jsonl."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True)

        # 300k / 1M = 30%
        transcript = _make_transcript(tmp_path, [
            {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 150_000,
                    "cache_creation_input_tokens": 100_000,
                    "cache_read_input_tokens": 50_000,
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

    def test_writes_inbox_warning_at_threshold(self, tmp_path):
        """Transcript usage at or above WARNING_THRESHOLD → inbox warning written."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        dedup_flag = tmp_path / "lobster-context-warning-sent"

        original_dedup = mod.DEDUP_FLAG
        mod.DEDUP_FLAG = dedup_flag
        try:
            # 800k / 1M = 80% → above 70% threshold
            transcript = _make_transcript(tmp_path, [
                {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 800_000,
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
        finally:
            mod.DEDUP_FLAG = original_dedup

        inbox_files = list(inbox_dir.glob("context-warning-*.json"))
        assert len(inbox_files) == 1
        msg = json.loads(inbox_files[0].read_text())
        assert msg["type"] == "context_warning"
        assert msg["used_percentage"] > WARNING_THRESHOLD

    def test_dedup_suppresses_second_warning(self, tmp_path):
        """Dedup flag present → second warning is not written."""
        mod = _load_hook()
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        dedup_flag = tmp_path / "lobster-context-warning-sent"
        dedup_flag.touch()  # Already flagged

        original_dedup = mod.DEDUP_FLAG
        mod.DEDUP_FLAG = dedup_flag
        try:
            transcript = _make_transcript(tmp_path, [
                {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 900_000,
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
        finally:
            mod.DEDUP_FLAG = original_dedup

        inbox_files = list(inbox_dir.glob("context-warning-*.json"))
        assert len(inbox_files) == 0, "Dedup flag should suppress second warning"

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
        """Missing transcript_path must never trigger a context_warning inbox message."""
        mod = _load_hook()
        inbox_dir = tmp_path / "messages" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        log_dir = tmp_path / "lobster-workspace" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        payload = {"tool_name": "mcp__lobster-inbox__mark_processed"}
        mod._handle_payload(payload, log_dir=log_dir, inbox_dir=inbox_dir)

        inbox_files = list(inbox_dir.glob("*.json"))
        assert len(inbox_files) == 0, (
            f"No inbox message should be written when transcript absent, "
            f"but found: {inbox_files}"
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
