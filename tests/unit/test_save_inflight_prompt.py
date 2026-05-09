"""
Unit tests for scripts/save-inflight-prompt.py (issue #1989 — Component 1).

## What this file tests

The save-inflight-prompt script must:
- Accept a JSON payload on stdin: {task_id, type, description, started_at,
  chat_id, subagent_type, status, prompt}
- Write the prompt text to ~/lobster-workspace/data/inflight-prompts/<task_id>.txt
- Append a JSONL entry to inflight-work.jsonl with all fields except prompt
  replaced by a prompt_file path pointing to the written file
- Handle the case where the inflight-prompts/ directory does not exist (create it)
- Handle missing or blank prompt gracefully (write empty file, still append JSONL)
- Be idempotent: if the same task_id is submitted again, overwrite the prompt
  file and append a new JSONL entry (append-only log semantics)
- Exit 0 on success, non-zero on fatal error (missing required fields)
- Never crash if the prompt file write fails — still append JSONL entry with
  prompt_file pointing to the intended path (best-effort)

## Named constants (spec-derived, not magic literals)

INFLIGHT_PROMPTS_DIR_NAME = "inflight-prompts"
INFLIGHT_WORK_FILENAME = "inflight-work.jsonl"
REQUIRED_FIELDS = ["task_id", "status"]
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Named constants matching those in the implementation
# ---------------------------------------------------------------------------

INFLIGHT_PROMPTS_DIR_NAME = "inflight-prompts"
INFLIGHT_WORK_FILENAME = "inflight-work.jsonl"
REQUIRED_FIELDS = ["task_id", "status"]

# ---------------------------------------------------------------------------
# Path to the script under test
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "save-inflight-prompt.py"


def _run_script(payload: dict, inflight_work: Path, inflight_prompts_dir: Path) -> subprocess.CompletedProcess:
    """Run the script with controlled file paths via env vars, payload via stdin."""
    env = {
        **os.environ,
        "LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE": str(inflight_work),
        "LOBSTER_INFLIGHT_PROMPTS_DIR_OVERRIDE": str(inflight_prompts_dir),
    }
    return subprocess.run(
        ["uv", "run", str(_SCRIPT_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read all entries from a JSONL file, skipping blank lines."""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


class TestSaveInflightPromptHappyPath:
    """Tests for the normal save-inflight-prompt.py operation."""

    def test_writes_prompt_to_file(self, tmp_path: Path) -> None:
        """Prompt text is written to inflight-prompts/<task_id>.txt."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "fix-pr-42",
            "type": "engineer",
            "description": "Fix PR #42 review comments",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 12345,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "---\ntask_id: fix-pr-42\nchat_id: 12345\n---\n\nFix the bug.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0, f"Script failed: {result.stderr}"

        prompt_file = prompts_dir / "fix-pr-42.txt"
        assert prompt_file.exists(), "Prompt file must be created"
        assert prompt_file.read_text() == payload["prompt"], (
            "Prompt file content must match the input prompt"
        )

    def test_appends_jsonl_entry_with_prompt_file_field(self, tmp_path: Path) -> None:
        """The JSONL entry includes prompt_file and subagent_type, not the prompt inline."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "fix-pr-42",
            "type": "engineer",
            "description": "Fix PR #42",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 12345,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "Do the task.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        entries = _read_jsonl(work_file)
        assert len(entries) == 1, "Exactly one JSONL entry must be appended"

        entry = entries[0]
        assert entry["task_id"] == "fix-pr-42"
        assert entry["status"] == "running"
        assert entry["subagent_type"] == "lobster-engineer"
        assert "prompt_file" in entry, "Entry must contain prompt_file field"
        assert "fix-pr-42.txt" in entry["prompt_file"], (
            "prompt_file must point to the task-specific file"
        )
        assert "prompt" not in entry, "Raw prompt must NOT be stored inline in the JSONL entry"

    def test_does_not_store_prompt_inline_in_jsonl(self, tmp_path: Path) -> None:
        """The raw prompt must not appear in the JSONL entry — only prompt_file."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "check-task",
            "type": "research",
            "description": "Check something",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-generalist",
            "status": "running",
            "prompt": "SECRET_CONTENT_12345",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        raw_content = work_file.read_text()
        assert "SECRET_CONTENT_12345" not in raw_content, (
            "Prompt text must not appear verbatim in inflight-work.jsonl"
        )

    def test_creates_inflight_prompts_dir_if_absent(self, tmp_path: Path) -> None:
        """inflight-prompts/ directory is created if it does not exist."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / "nonexistent-dir" / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "new-task",
            "type": "engineer",
            "description": "A task",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "Do the thing.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0
        assert prompts_dir.exists(), "Directory must be created if absent"
        assert (prompts_dir / "new-task.txt").exists()

    def test_creates_inflight_work_jsonl_if_absent(self, tmp_path: Path) -> None:
        """inflight-work.jsonl is created (appended to) even if it does not exist."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        assert not work_file.exists()

        payload = {
            "task_id": "first-task",
            "type": "engineer",
            "description": "First",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 99,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "Hello.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0
        assert work_file.exists(), "inflight-work.jsonl must be created on first write"
        entries = _read_jsonl(work_file)
        assert len(entries) == 1

    def test_preserves_all_metadata_fields_in_jsonl(self, tmp_path: Path) -> None:
        """task_id, type, description, started_at, chat_id, status are all in the JSONL entry."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "full-task",
            "type": "reviewer",
            "description": "Review the PR",
            "started_at": "2026-05-09T14:30:00Z",
            "chat_id": 8305714125,
            "subagent_type": "review",
            "status": "running",
            "prompt": "Review PR #55.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        entry = _read_jsonl(work_file)[0]
        assert entry["task_id"] == "full-task"
        assert entry["type"] == "reviewer"
        assert entry["description"] == "Review the PR"
        assert entry["started_at"] == "2026-05-09T14:30:00Z"
        assert entry["chat_id"] == 8305714125
        assert entry["subagent_type"] == "review"
        assert entry["status"] == "running"

    def test_multiline_prompt_written_correctly(self, tmp_path: Path) -> None:
        """Multiline prompts (with newlines, quotes, special chars) are preserved exactly."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        multiline = (
            "---\ntask_id: ml-task\nchat_id: 0\nsource: system\nbackground: true\n---\n\n"
            'Fix the "bug" with special chars: {\'key\': \'value\'} and newlines\n'
            "Line 2 of prompt.\n"
            "Line 3 with unicode: — é 中文\n"
        )
        payload = {
            "task_id": "ml-task",
            "type": "engineer",
            "description": "Multiline test",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": multiline,
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        prompt_file = prompts_dir / "ml-task.txt"
        assert prompt_file.read_text(encoding="utf-8") == multiline, (
            "Multiline prompt must be written verbatim"
        )

    def test_appends_multiple_entries_to_existing_file(self, tmp_path: Path) -> None:
        """Multiple calls append to the existing JSONL file rather than overwriting it."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME

        for i in range(3):
            payload = {
                "task_id": f"task-{i}",
                "type": "engineer",
                "description": f"Task {i}",
                "started_at": "2026-05-09T12:00:00Z",
                "chat_id": i,
                "subagent_type": "lobster-engineer",
                "status": "running",
                "prompt": f"Prompt for task {i}.",
            }
            result = _run_script(payload, work_file, prompts_dir)
            assert result.returncode == 0, f"Call {i} failed: {result.stderr}"

        entries = _read_jsonl(work_file)
        assert len(entries) == 3, "Three sequential calls must produce three JSONL entries"
        task_ids = {e["task_id"] for e in entries}
        assert task_ids == {"task-0", "task-1", "task-2"}


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestSaveInflightPromptEdgeCases:
    """Edge case and resilience tests."""

    def test_blank_prompt_writes_empty_file(self, tmp_path: Path) -> None:
        """A blank or missing prompt writes an empty prompt file; JSONL entry still appended."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "no-prompt-task",
            "type": "engineer",
            "description": "No prompt",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            # prompt field absent
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        entries = _read_jsonl(work_file)
        assert len(entries) == 1, "JSONL entry must be appended even with no prompt"
        assert entries[0]["task_id"] == "no-prompt-task"

        prompt_file = prompts_dir / "no-prompt-task.txt"
        assert prompt_file.exists(), "Prompt file must be created even for empty prompt"
        assert prompt_file.read_text() == "", "Empty prompt writes an empty file"

    def test_exits_nonzero_on_missing_task_id(self, tmp_path: Path) -> None:
        """Missing task_id causes non-zero exit (task_id is required to name the prompt file)."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "type": "engineer",
            "description": "No task_id",
            "status": "running",
            "prompt": "Oops.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode != 0, "Must exit non-zero when task_id is missing"

    def test_exits_nonzero_on_missing_status(self, tmp_path: Path) -> None:
        """Missing status causes non-zero exit (status is required for JSONL semantics)."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "no-status-task",
            "type": "engineer",
            "description": "No status",
            "prompt": "Oops.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode != 0, "Must exit non-zero when status is missing"

    def test_overwrites_prompt_file_on_same_task_id(self, tmp_path: Path) -> None:
        """Re-submitting the same task_id overwrites the prompt file (idempotent)."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME

        base_payload = {
            "task_id": "idempotent-task",
            "type": "engineer",
            "description": "Idempotent task",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
        }

        # First run
        result1 = _run_script({**base_payload, "prompt": "First prompt."}, work_file, prompts_dir)
        assert result1.returncode == 0

        # Second run with same task_id
        result2 = _run_script({**base_payload, "prompt": "Second prompt."}, work_file, prompts_dir)
        assert result2.returncode == 0

        # Prompt file has the second prompt
        prompt_file = prompts_dir / "idempotent-task.txt"
        assert prompt_file.read_text() == "Second prompt.", (
            "Prompt file must be overwritten on re-submission of same task_id"
        )

        # JSONL has two entries (append-only)
        entries = _read_jsonl(work_file)
        assert len(entries) == 2, "Two JSONL entries are appended (append-only semantics)"

    def test_path_traversal_task_id_raises(self, tmp_path: Path) -> None:
        """A task_id containing path traversal characters must raise ValueError (non-zero exit)."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "../evil",
            "type": "engineer",
            "description": "Traversal attempt",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "evil content",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode != 0, (
            "Must exit non-zero when task_id contains path traversal characters"
        )
        # No files should have been written outside the prompts_dir
        assert not (tmp_path / "evil.txt").exists(), (
            "Path traversal must not write outside the prompts directory"
        )

    def test_exits_nonzero_on_invalid_json_stdin(self, tmp_path: Path) -> None:
        """Invalid JSON on stdin causes non-zero exit."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        env = {
            **os.environ,
            "LOBSTER_INFLIGHT_WORK_FILE_OVERRIDE": str(work_file),
            "LOBSTER_INFLIGHT_PROMPTS_DIR_OVERRIDE": str(prompts_dir),
        }
        result = subprocess.run(
            ["uv", "run", str(_SCRIPT_PATH)],
            input="NOT VALID JSON",
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, "Must exit non-zero on invalid JSON stdin"


# ---------------------------------------------------------------------------
# Tests: prompt_file path format
# ---------------------------------------------------------------------------


class TestPromptFilePath:
    """Tests for the prompt_file path stored in the JSONL entry."""

    def test_prompt_file_path_is_absolute(self, tmp_path: Path) -> None:
        """prompt_file in JSONL entry must be an absolute path."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "abs-path-task",
            "type": "engineer",
            "description": "Test",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "Hello.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        entry = _read_jsonl(work_file)[0]
        assert Path(entry["prompt_file"]).is_absolute(), (
            "prompt_file must be an absolute path"
        )

    def test_prompt_file_path_matches_actual_file(self, tmp_path: Path) -> None:
        """The prompt_file path in JSONL entry must point to the actual file written."""
        work_file = tmp_path / INFLIGHT_WORK_FILENAME
        prompts_dir = tmp_path / INFLIGHT_PROMPTS_DIR_NAME
        payload = {
            "task_id": "path-check-task",
            "type": "engineer",
            "description": "Path check",
            "started_at": "2026-05-09T12:00:00Z",
            "chat_id": 0,
            "subagent_type": "lobster-engineer",
            "status": "running",
            "prompt": "Prompt content.",
        }
        result = _run_script(payload, work_file, prompts_dir)
        assert result.returncode == 0

        entry = _read_jsonl(work_file)[0]
        prompt_path = Path(entry["prompt_file"])
        assert prompt_path.exists(), "prompt_file path must point to an existing file"
        assert prompt_path.read_text() == "Prompt content.", (
            "File at prompt_file path must contain the original prompt"
        )
