#!/usr/bin/env python3
"""save-inflight-prompt.py — persist full Agent prompt for killed-subagent recovery.

Part of issue #1989 — Automatic subagent restart after dispatcher session death.

## Purpose

When the dispatcher spawns a background subagent via the Agent tool, it must
persist the full prompt so that if the dispatcher session dies (health-check
kill, OOM, crash), the new dispatcher can recover and relaunch the killed
subagent automatically.

## Why not store the prompt inline in inflight-work.jsonl?

The existing inflight-work.jsonl is written via Bash `echo '...' >> file`.
Prompts are multi-line strings containing quotes, braces, and special
characters that make shell escaping unreliable and fragile. Instead:
- The prompt text is written to a separate file:
    ~/lobster-workspace/data/inflight-prompts/<task_id>.txt
- The JSONL entry adds `prompt_file` (path) and `subagent_type` fields.
  The raw prompt text is NEVER stored inline in the JSONL.

## Usage

```bash
echo '{"task_id": "fix-pr-42", "type": "engineer", "description": "...",
       "started_at": "2026-05-09T12:00:00Z", "chat_id": 12345,
       "subagent_type": "lobster-engineer", "status": "running",
       "prompt": "---\\ntask_id: fix-pr-42\\n..."}' | uv run scripts/save-inflight-prompt.py
```

## Input (JSON on stdin)

Required fields:
  task_id   — unique identifier for this subagent task
  status    — "running" (this script is only called on spawn; "done" entries are
              written by the dispatcher inline as before)

Optional fields:
  type          — task type label (e.g. "engineer", "reviewer")
  description   — brief human-readable description
  started_at    — ISO UTC timestamp (e.g. "2026-05-09T12:00:00Z")
  chat_id       — originating chat ID (0 for system tasks)
  subagent_type — the subagent type passed to the Agent tool
  prompt        — full prompt text (may be multi-line, any encoding)

## Output

Exit 0 on success. Non-zero on fatal error (missing required fields, invalid
JSON). Always silent on all non-fatal errors (e.g. prompt file write failure)
— those are logged to stderr but do not affect the JSONL append.

## Environment overrides (for tests)

  LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE   — override the inflight-work.jsonl path
  LOBSTER_INFLIGHT_PROMPTS_DIR_OVERRIDE — override the inflight-prompts/ dir path
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Named constants (spec-derived, not magic literals)
# ---------------------------------------------------------------------------

# Required fields that must be present in the input payload.
REQUIRED_FIELDS: tuple[str, ...] = ("task_id", "status")

# Default paths (can be overridden by env vars for testability).
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))

INFLIGHT_WORK_FILE: Path = Path(
    os.environ.get(
        "LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE",
        str(_WORKSPACE / "data" / "inflight-work.jsonl"),
    )
)

INFLIGHT_PROMPTS_DIR: Path = Path(
    os.environ.get(
        "LOBSTER_INFLIGHT_PROMPTS_DIR_OVERRIDE",
        str(_WORKSPACE / "data" / "inflight-prompts"),
    )
)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def validate_payload(payload: dict) -> list[str]:
    """Return a list of validation error messages for the input payload.

    Returns an empty list if the payload is valid. Pure function — no I/O.
    """
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in payload:
            errors.append(f"missing required field: {field!r}")
    return errors


def build_jsonl_entry(payload: dict, prompt_file_path: Path) -> dict:
    """Return the JSONL entry dict for inflight-work.jsonl.

    The raw prompt is NOT included — only the path to the prompt file.
    All metadata fields from the payload are carried through.

    Pure function — no I/O.
    """
    entry: dict = {}

    # Copy all payload fields except 'prompt' (stored in a separate file).
    for key, value in payload.items():
        if key != "prompt":
            entry[key] = value

    # Add prompt_file field pointing to the written file.
    entry["prompt_file"] = str(prompt_file_path)

    return entry


# ---------------------------------------------------------------------------
# I/O functions (isolated side effects)
# ---------------------------------------------------------------------------


def write_prompt_file(prompts_dir: Path, task_id: str, prompt: str) -> Path:
    """Write the prompt text to prompts_dir/<task_id>.txt atomically.

    Creates prompts_dir if it does not exist.
    Overwrites any existing file with the same task_id (idempotent).
    Returns the absolute path of the written file.

    Raises OSError on write failure.
    """
    if "/" in task_id or task_id.startswith("."):
        raise ValueError(f"task_id contains path traversal characters: {task_id!r}")
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = prompts_dir / f"{task_id}.txt"

    # Atomic write: temp file in same dir + rename.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(prompts_dir),
        prefix=f".{task_id}-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        os.replace(tmp_path, str(prompt_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return prompt_file.resolve()


def append_jsonl_entry(work_file: Path, entry: dict) -> None:
    """Append a JSON line to inflight-work.jsonl.

    Creates parent directories if they do not exist.
    Uses O_APPEND semantics — atomic for payloads < PIPE_BUF (4096 bytes).
    Single JSONL entries are well under that limit.

    Raises OSError on write failure.
    """
    work_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(work_file, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Parse stdin, write prompt file, append JSONL entry.

    Returns 0 on success, 1 on invalid input, 2 on unexpected error.
    """
    # 1. Parse stdin as JSON.
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[save-inflight-prompt] fatal: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print(
            f"[save-inflight-prompt] fatal: expected JSON object, got {type(payload).__name__}",
            file=sys.stderr,
        )
        return 1

    # 2. Validate required fields.
    errors = validate_payload(payload)
    if errors:
        for err in errors:
            print(f"[save-inflight-prompt] fatal: {err}", file=sys.stderr)
        return 1

    task_id: str = payload["task_id"]
    prompt: str = payload.get("prompt", "")

    # 3. Write the prompt to a dedicated file (best-effort — JSONL still appended on failure).
    #    ValueError (e.g. path traversal) is fatal and causes immediate exit.
    prompt_file_path = INFLIGHT_PROMPTS_DIR / f"{task_id}.txt"
    try:
        prompt_file_path = write_prompt_file(INFLIGHT_PROMPTS_DIR, task_id, prompt)
    except ValueError as exc:
        print(
            f"[save-inflight-prompt] fatal: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(
            f"[save-inflight-prompt] warning: failed to write prompt file for {task_id!r}: {exc}",
            file=sys.stderr,
        )
        # Continue — JSONL entry still appended with the intended path.
        prompt_file_path = (INFLIGHT_PROMPTS_DIR / f"{task_id}.txt").resolve()

    # 4. Build and append the JSONL entry.
    entry = build_jsonl_entry(payload, prompt_file_path)
    try:
        append_jsonl_entry(INFLIGHT_WORK_FILE, entry)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[save-inflight-prompt] fatal: failed to append JSONL entry for {task_id!r}: {exc}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
