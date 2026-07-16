#!/usr/bin/env python3
"""
PostToolUse hook: per-subagent token usage accumulator.

Fires after every Agent tool call. Reads the completed subagent's JSONL
transcript, sums all token counts across turns, and appends a record to
~/lobster-workspace/logs/token-usage.jsonl.

## Data source

Subagent transcripts live at:
  ~/.claude/projects/-home-lobster-lobster-workspace/<session_id>/subagents/agent-<agentId>.jsonl

These files are flushed when the agent returns, so the PostToolUse hook
fires after the data is available.

## Output format

Each record appended to token-usage.jsonl:
  {
    "ts": "ISO UTC",
    "task_id": "...",
    "agent_id": "agent-xxx",
    "subagent_type": "...",
    "chat_id": "12345",
    "model": "claude-sonnet-4-6",
    "input_tokens": 850000,
    "cache_creation_tokens": 50000,
    "cache_read_tokens": 800000,
    "output_tokens": 4200,
    "turns": 22,
    "session_id": "..."
  }

## Failure policy

Any error exits 0 and appends to hook-failures.log. Never blocks the Agent call.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOME = Path.home()
_WORKSPACE = Path.home() / "lobster-workspace"
_LOG_DIR = _WORKSPACE / "logs"
_TOKEN_USAGE_LOG = _LOG_DIR / "token-usage.jsonl"
_FAILURES_LOG = _LOG_DIR / "hook-failures.log"
_CLAUDE_PROJECTS = _HOME / ".claude" / "projects" / "-home-lobster-lobster-workspace"

# Retry config for subagent JSONL availability
_MAX_RETRIES = 3
_RETRY_DELAY_S = 0.2


# ---------------------------------------------------------------------------
# Logging (failures only)
# ---------------------------------------------------------------------------

def _log_failure(message: str) -> None:
    """Append a timestamped failure entry to hook-failures.log."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with _FAILURES_LOG.open("a") as f:
            f.write(f"[{ts}] usage-accumulator: {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Frontmatter / metadata extraction (shared pattern with auto-register-agent)
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(prompt: str) -> dict:
    """Extract key/value pairs from a YAML frontmatter block."""
    prompt = prompt.lstrip()
    if not prompt.startswith("---"):
        return {}
    rest = prompt[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    block = rest[:end].strip()
    result = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def _extract_task_id_from_text(prompt: str) -> str | None:
    """Fall back: extract task_id from 'task_id is: X' pattern."""
    match = re.search(r"task_id\s+is:\s*(\S+)", prompt, re.IGNORECASE)
    return match.group(1) if match else None


def extract_metadata(prompt: str) -> dict:
    """Return task_id, chat_id, source, subagent_type from prompt frontmatter."""
    fm = _parse_yaml_frontmatter(prompt)
    task_id = fm.get("task_id") or _extract_task_id_from_text(prompt)
    chat_id = fm.get("chat_id")
    source = fm.get("source", "telegram")
    subagent_type = fm.get("subagent_type")
    return {
        "task_id": task_id,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "source": source or "telegram",
        "subagent_type": subagent_type,
    }


# ---------------------------------------------------------------------------
# Agent ID extraction
# ---------------------------------------------------------------------------

def extract_agent_id(tool_response: object) -> str | None:
    """Extract agentId from the Agent tool response (dict or list of items)."""
    if isinstance(tool_response, dict):
        agent_id = tool_response.get("agentId")
        if agent_id:
            return str(agent_id)
    if isinstance(tool_response, list):
        for item in tool_response:
            if isinstance(item, dict):
                agent_id = item.get("agentId")
                if agent_id:
                    return str(agent_id)
    return None


def extract_agent_type(tool_response: object) -> str | None:
    """Extract agentType from the Agent tool response if available."""
    if isinstance(tool_response, dict):
        return tool_response.get("agentType") or tool_response.get("subagent_type")
    if isinstance(tool_response, list):
        for item in tool_response:
            if isinstance(item, dict):
                t = item.get("agentType") or item.get("subagent_type")
                if t:
                    return str(t)
    return None


# ---------------------------------------------------------------------------
# Subagent JSONL parsing
# ---------------------------------------------------------------------------

def _locate_subagent_jsonl(session_id: str, agent_id: str) -> Path:
    """Resolve the path to agent-<agentId>.jsonl within the session directory."""
    session_dir = _CLAUDE_PROJECTS / session_id / "subagents"
    return session_dir / f"agent-{agent_id}.jsonl"


def _wait_for_jsonl(path: Path, max_retries: int = _MAX_RETRIES, delay: float = _RETRY_DELAY_S) -> bool:
    """Wait up to max_retries * delay seconds for the JSONL file to exist and be non-empty."""
    for _ in range(max_retries):
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(delay)
    return path.exists() and path.stat().st_size > 0


def _sum_jsonl_tokens(path: Path) -> dict:
    """Read all assistant turns from a subagent JSONL and sum token counts.

    Returns a dict with: input_tokens, cache_creation_tokens, cache_read_tokens,
    output_tokens, turns, model.

    Sums all turns so we get the total token expenditure across the full
    subagent lifetime, not just the last turn.
    """
    input_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    output_tokens = 0
    turns = 0
    model = "unknown"

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                # Subagent transcripts use: {type: "assistant", message: {role, usage, model}}
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("role") == "assistant" and "usage" in msg:
                        usage = msg["usage"] or {}
                        turns += 1
                        input_tokens += usage.get("input_tokens", 0) or 0
                        cache_creation_tokens += usage.get("cache_creation_input_tokens", 0) or 0
                        cache_read_tokens += usage.get("cache_read_input_tokens", 0) or 0
                        output_tokens += usage.get("output_tokens", 0) or 0
                        # Keep last model seen
                        m = msg.get("model")
                        if m:
                            model = m
    except OSError as exc:
        raise RuntimeError(f"failed to read {path}: {exc}") from exc

    return {
        "input_tokens": input_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
        "turns": turns,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Meta.json parsing (for subagent_type)
# ---------------------------------------------------------------------------

def _read_meta_json(session_id: str, agent_id: str) -> dict:
    """Read agent-<agentId>.meta.json if present, returning {} on failure."""
    meta_path = _CLAUDE_PROJECTS / session_id / "subagents" / f"agent-{agent_id}.meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Output: append to token-usage.jsonl
# ---------------------------------------------------------------------------

def _append_usage_record(record: dict) -> None:
    """Atomically append a JSON line to token-usage.jsonl."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _TOKEN_USAGE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _build_usage_record(
    *,
    ts: str,
    task_id: str | None,
    agent_id: str,
    subagent_type: str | None,
    chat_id: str | None,
    model: str,
    input_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    turns: int,
    session_id: str,
) -> dict:
    """Construct a token usage record as an immutable dict."""
    return {
        "ts": ts,
        "task_id": task_id,
        "agent_id": f"agent-{agent_id}",
        "subagent_type": subagent_type,
        "chat_id": chat_id,
        "model": model,
        "input_tokens": input_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "output_tokens": output_tokens,
        "turns": turns,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def _handle_payload(data: dict) -> None:
    """Process a single PostToolUse payload for the Agent tool."""
    tool_name = data.get("tool_name", "")
    if tool_name != "Agent":
        return

    tool_input = data.get("tool_input", {})
    tool_response = data.get("tool_response")
    session_id = data.get("session_id", "")

    agent_id = extract_agent_id(tool_response)
    if not agent_id:
        return

    prompt = tool_input.get("prompt", "")
    metadata = extract_metadata(prompt)

    # Try to get subagent_type from: (1) meta.json, (2) tool_response, (3) prompt frontmatter
    meta = _read_meta_json(session_id, agent_id)
    subagent_type = (
        meta.get("agentType")
        or extract_agent_type(tool_response)
        or metadata.get("subagent_type")
    )

    # Locate and wait for subagent JSONL
    jsonl_path = _locate_subagent_jsonl(session_id, agent_id)

    if not _wait_for_jsonl(jsonl_path):
        _log_failure(f"JSONL not available after retries: {jsonl_path}")
        return

    try:
        token_data = _sum_jsonl_tokens(jsonl_path)
    except RuntimeError as exc:
        _log_failure(str(exc))
        return

    ts = datetime.now(timezone.utc).isoformat()
    record = _build_usage_record(
        ts=ts,
        task_id=metadata["task_id"],
        agent_id=agent_id,
        subagent_type=subagent_type,
        chat_id=metadata["chat_id"],
        model=token_data["model"],
        input_tokens=token_data["input_tokens"],
        cache_creation_tokens=token_data["cache_creation_tokens"],
        cache_read_tokens=token_data["cache_read_tokens"],
        output_tokens=token_data["output_tokens"],
        turns=token_data["turns"],
        session_id=session_id,
    )

    _append_usage_record(record)


def main() -> None:
    try:
        data = json.load(sys.stdin)
        _handle_payload(data)
    except Exception as exc:
        _log_failure(f"unexpected error: {exc}")
    sys.exit(0)


if __name__ == "__main__":
    main()
