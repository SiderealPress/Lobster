#!/usr/bin/env python3
"""
PostToolUse hook: context window monitor.

Reads actual token usage from the session's transcript JSONL file rather than
relying on the `context_window` field in the PostToolUse payload — which Claude
Code never populates for PostToolUse events (confirmed in production and SDK type
analysis; tracked upstream in anthropics/claude-code#32014 / #35059).

Approach:
  1. Extract `transcript_path` from the hook payload.
  2. Read the JSONL file line-by-line to find the last assistant turn that
     contains a `message.usage` block.
  3. Compute total tokens = input_tokens + cache_creation_input_tokens
     + cache_read_input_tokens.
  4. Look up the model's max context window from a known table (200k for all
     current CC models; see MODEL_CONTEXT_SIZES for details).
  5. Log used_pct to context-monitor.log.

No threshold triggering, no wind-down mode, no inbox messages. CC compacts on
its own terms; the on-compact.py hook handles recovery after each compaction.
(issue #2056: wind-down mode removed)
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

# Known model context sizes. Matched by prefix so versioned IDs like
# 'claude-haiku-4-5-20251001' resolve correctly.
# Default fallback: 200_000 (conservative — avoids false negatives on unknown models).
MODEL_CONTEXT_SIZES: list[tuple[str, int]] = [
    # claude-sonnet-4-6 supports up to 1M tokens but CC's default window is 200k.
    # Update when we can detect which mode is active.
    ("claude-sonnet-4-6", 200_000),
    # claude-opus-4-6 supports up to 1M tokens but CC's default window is 200k.
    # Update when we can detect which mode is active.
    ("claude-opus-4-6", 200_000),
    ("claude-haiku-4-5", 200_000),
]
DEFAULT_CONTEXT_SIZE = 200_000


def _model_max_context(model: str) -> int:
    """Return the max context window size for a known model ID.

    Matches by prefix to handle versioned IDs like 'claude-haiku-4-5-20251001'.
    Falls back to DEFAULT_CONTEXT_SIZE for unknown models.
    """
    for prefix, size in MODEL_CONTEXT_SIZES:
        if model.startswith(prefix):
            return size
    return DEFAULT_CONTEXT_SIZE


def _read_transcript_usage(
    transcript_path: str | None,
) -> "tuple[float, float, str, int] | None":
    """Read the last assistant usage entry from the transcript JSONL.

    Returns (used_pct, remaining_pct, model, total_tokens) or None if data
    is unavailable. total_tokens is the raw token count (input + cache), which
    is always accurate regardless of the assumed max context window.

    Reads the file line-by-line, keeping only the last assistant entry with a
    usage block. This is O(n) in lines but O(1) in memory — suitable for large
    transcripts. The last entry is authoritative: it reflects the most recent
    context state.

    Transcript format (each line is a JSON object):
      {
        "type": "assistant",
        "message": {
          "role": "assistant",
          "model": "claude-sonnet-4-6",
          "usage": {
            "input_tokens": N,
            "cache_creation_input_tokens": N,
            "cache_read_input_tokens": N,
            ...
          }
        }
      }
    """
    if not transcript_path:
        return None

    path = Path(transcript_path)
    if not path.exists():
        return None

    last_usage: dict | None = None
    last_model: str = "unknown"

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # Assistant turns are wrapped: {type: "assistant", message: {...}}
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant" and "usage" in msg:
                        last_usage = msg["usage"]
                        last_model = msg.get("model", "unknown")
    except OSError:
        return None

    if last_usage is None:
        return None

    input_tokens = last_usage.get("input_tokens", 0) or 0
    cache_create = last_usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = last_usage.get("cache_read_input_tokens", 0) or 0
    total_used = input_tokens + cache_create + cache_read

    model_max = _model_max_context(last_model)
    used_pct = (total_used / model_max) * 100.0
    remaining_pct = 100.0 - used_pct

    return (used_pct, remaining_pct, last_model, total_used)


def _log_usage(log_dir: Path, entry: dict) -> None:
    """Append a usage entry to the context-monitor log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "context-monitor.log"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _build_log_entry(
    tool_name: str,
    used_pct: float,
    remaining_pct: float,
    model: str,
) -> dict:
    """Return an immutable log entry dict from computed usage data."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "used_percentage": used_pct,
        "remaining_percentage": remaining_pct,
        "model": model,
        "source": "transcript_jsonl",
    }


def _build_absent_context_entry(tool_name: str, reason: str = "") -> dict:
    """Return a WARN log entry for when transcript usage data is unavailable.

    Preserves the 'hook fired, no data' vs 'hook never fired' distinction
    so the log stays useful even when the transcript path is absent.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "transcript_unavailable": True,
        "warn": f"[WARN] transcript usage unavailable for tool: {tool_name}",
        "reason": reason,
    }


def _handle_payload(
    data: dict,
    log_dir: Path | None = None,
    inbox_dir: Path | None = None,
) -> None:
    """Process a single PostToolUse payload.

    Accepts log_dir and inbox_dir as injectable parameters so tests can verify
    behavior without touching the real filesystem. When not provided, defaults
    to the standard runtime paths. inbox_dir is accepted but unused — retained
    for test compatibility (issue #2056: inbox warning removed with wind-down mode).
    """
    if log_dir is None:
        log_dir = Path.home() / "lobster-workspace" / "logs"

    tool_name = data.get("tool_name", "unknown")
    transcript_path = data.get("transcript_path")

    result = _read_transcript_usage(transcript_path)

    if result is None:
        # Hook fired but usage data is unavailable (no transcript_path, file
        # missing, or no assistant turns yet). Log WARN so the log distinguishes
        # "no data" from "hook never fired."
        reason = (
            "transcript_path absent"
            if not transcript_path
            else "no assistant usage found in transcript"
        )
        entry = _build_absent_context_entry(tool_name, reason)
        _log_usage(log_dir, entry)
        return

    used_pct, remaining_pct, model, _total_tokens = result

    entry = _build_log_entry(tool_name, used_pct, remaining_pct, model)
    _log_usage(log_dir, entry)

    # No threshold check, no inbox message, no wind-down triggering.
    # CC compacts on its own terms; on-compact.py handles post-compaction recovery.


def main() -> None:
    try:
        data = json.load(sys.stdin)
        _handle_payload(data)
    except Exception:
        pass  # Never block tool use


if __name__ == "__main__":
    main()
